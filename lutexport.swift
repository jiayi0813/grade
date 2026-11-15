// GPU video export for app.py: applies a .cube LUT to a stretch of video and encodes it as
// 10-bit HEVC, with decoding, the LUT and encoding all on Apple hardware. Video only; app.py
// adds the audio with ffmpeg. Optionally removes noise before the LUT and adds film grain after
// it. Built on demand by app.py:
//   swiftc -O -swift-version 5 lutexport.swift -o lutexport
//   lutexport input start duration lut.cube output.mov width height denoise grain grain-size
// denoise and grain are strengths from 0 (off) to 1; grain-size is in output pixels.
import AVFoundation
import CoreImage
import VideoToolbox

func fail(_ msg: String) -> Never { FileHandle.standardError.write((msg + "\n").data(using: .utf8)!); exit(1) }

let a = CommandLine.arguments
guard a.count == 11, let start = Double(a[2]), let length = Double(a[3]), let w = Int(a[6]), let h = Int(a[7]),
      let denoise = Double(a[8]), let grain = Double(a[9]), let grainSize = Double(a[10]), grainSize > 0
else { fail("usage: lutexport input start duration lut.cube output.mov width height denoise grain grain-size") }

// ---- LUT: .cube rows are RGB with red fastest, which is the order Core Image wants (as RGBA)
var size = 0
var cube = [Float]()
guard let text = try? String(contentsOfFile: a[4], encoding: .utf8) else { fail("cannot read LUT") }
for line in text.split(separator: "\n") {
    let parts = line.split(separator: " ")
    if parts.first == "LUT_3D_SIZE" { size = Int(parts[1]) ?? 0 }
    else if parts.count == 3, let r = Float(parts[0]), let g = Float(parts[1]), let b = Float(parts[2]) { cube += [r, g, b, 1] }
}
guard size > 1, size <= 64, cube.count == size * size * size * 4 else { fail("LUT must be a 3D .cube of at most 64 points a side") }
let filter = CIFilter(name: "CIColorCube")!
filter.setValue(size, forKey: "inputCubeDimension")
filter.setValue(cube.withUnsafeBufferPointer { Data(buffer: $0) }, forKey: "inputCubeData")

// ---- reader: decoded 10-bit frames that stay on the GPU
let asset = AVURLAsset(url: URL(fileURLWithPath: a[1]))
guard let track = asset.tracks(withMediaType: .video).first else { fail("no readable video track") }
let buffers: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange,
                              kCVPixelBufferMetalCompatibilityKey as String: true]
guard let reader = try? AVAssetReader(asset: asset) else { fail("cannot read video") }
let from = CMTime(seconds: start, preferredTimescale: 60000), to = CMTime(seconds: start + length, preferredTimescale: 60000)
// Read from a little early: the reader clamps a frame that straddles the start of its range
// to the range, which would let the frame before the cut through the test in the loop below.
reader.timeRange = CMTimeRange(start: max(.zero, from - CMTime(seconds: 0.5, preferredTimescale: 600)), end: to)
let output = AVAssetReaderTrackOutput(track: track, outputSettings: buffers)
output.alwaysCopiesSampleData = false
reader.add(output)

// ---- geometry: cameras record a turn (e.g. mounted upside down) as a track transform, not in
// the pixels. After turning, the frame is scaled to fit the output and centred on black.
let t = track.preferredTransform
let turn = Int((atan2(t.b, t.a) * 180 / .pi).rounded())
let orientation: CGImagePropertyOrientation = [90: .right, -90: .left, 180: .down, -180: .down][turn] ?? .up
let natural = track.naturalSize
let shown = abs(turn) == 90 ? CGSize(width: natural.height, height: natural.width) : natural
let scale = min(CGFloat(w) / shown.width, CGFloat(h) / shown.height)
let fit = CGAffineTransform(scaleX: scale, y: scale).concatenating(CGAffineTransform(
    translationX: ((CGFloat(w) - shown.width * scale) / 2).rounded(), y: ((CGFloat(h) - shown.height * scale) / 2).rounded()))
let canvas = CGRect(x: 0, y: 0, width: w, height: h)
let black = CIImage(color: .black).cropped(to: canvas)
let resize = Int(shown.width) != w || Int(shown.height) != h

// ---- denoise: each pixel is averaged with the same pixel in the two frames before and after,
// but only with those that look alike, so things that move are left alone (no ghosting).
let calm = CIColorKernel(source: """
    kernel vec4 calm(__sample c, __sample p, __sample q, __sample r, __sample s, float k) {
        vec4 sum = c; float n = 1.0; vec3 d; float w;
        d = p.rgb - c.rgb; w = exp(-dot(d, d) * k); sum += p * w; n += w;
        d = q.rgb - c.rgb; w = exp(-dot(d, d) * k); sum += q * w; n += w;
        d = r.rgb - c.rgb; w = exp(-dot(d, d) * k); sum += r * w; n += w;
        d = s.rgb - c.rgb; w = exp(-dot(d, d) * k); sum += s * w; n += w;
        return sum / n;
    }
    """)!
let reach = denoise > 0 ? 2 : 0  // neighbouring frames used on each side

// ---- grain: grey noise (four uniform channels summed, so roughly bell-shaped) added to the
// picture in proportion to l(1 - l): mid-tones get the most, black and white none
let speckle = CIColorKernel(source: """
    kernel vec4 speckle(__sample image, __sample noise, float amount) {
        float l = clamp(dot(image.rgb, vec3(0.2126, 0.7152, 0.0722)), 0.0, 1.0);
        float n = noise.r + noise.g + noise.b + noise.a - 2.0;
        return vec4(image.rgb + n * amount * 4.0 * l * (1.0 - l), image.a);
    }
    """)!
let noise = CIFilter(name: "CIRandomGenerator")!.outputImage!

// ---- writer: hardware HEVC Main 10, tagged Rec.709
let rec709 = [AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
              AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
              AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2]
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.hevc, AVVideoWidthKey: w, AVVideoHeightKey: h,
    AVVideoColorPropertiesKey: rec709,
    AVVideoCompressionPropertiesKey: [AVVideoProfileLevelKey: kVTProfileLevel_HEVC_Main10_AutoLevel as String,
                                      AVVideoQualityKey: 0.55]])  // same as ffmpeg's -q:v 55 in app.py
input.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes:
    buffers.merging([kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h]) { x, _ in x })
try? FileManager.default.removeItem(atPath: a[5])
guard let writer = try? AVAssetWriter(outputURL: URL(fileURLWithPath: a[5]), fileType: .mov) else { fail("cannot write output") }
writer.add(input)

// ---- no colour management: the LUT must see the encoded Rec.709 values, as it does in the preview
let none = NSNull()
let context = CIContext(options: [.workingColorSpace: none, .outputColorSpace: none,
                                  .workingFormat: CIFormat.RGBAh, .cacheIntermediates: false])
guard reader.startReading(), writer.startWriting() else { fail("\(reader.error ?? writer.error!)") }

var frames = 0
var queue = [(CVPixelBuffer, CMTime)](), past = [CVPixelBuffer]()
// Each frame runs in its own autorelease pool. Without one, frames are never handed back to
// the decoder, which then fails with "Cannot Decode" after about 77 seconds of video.
func step() -> Bool {
    while queue.count <= reach, let sample = output.copyNextSampleBuffer() {  // the frame plus those the denoiser looks ahead to
        if let buffer = CMSampleBufferGetImageBuffer(sample) { queue.append((buffer, CMSampleBufferGetPresentationTimeStamp(sample))) }
    }
    guard !queue.isEmpty else { return false }
    let (src, time) = queue.removeFirst()
    defer { past = Array((past + [src]).suffix(reach)) }
    if time < from || time >= to { return true }  // the same frames ffmpeg's -ss / -t would keep
    if frames == 0 { writer.startSession(atSourceTime: time) }
    while !input.isReadyForMoreMediaData { usleep(500) }
    var made: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &made)
    guard let dst = made else { fail("out of video memory") }
    func picture(_ buffer: CVPixelBuffer) -> CIImage { CIImage(cvPixelBuffer: buffer, options: [.colorSpace: none]) }
    var frame = picture(src)
    if denoise > 0 {
        let t = denoise * 0.05  // how different a neighbour's pixel may be and still count
        let near = (past + queue.map { $0.0 }).map(picture) + Array(repeating: frame, count: 4)  // short at the ends: itself
        frame = calm.apply(extent: frame.extent, arguments: [frame, near[0], near[1], near[2], near[3], 1 / (2 * t * t)])!
    }
    filter.setValue(frame.oriented(orientation), forKey: kCIInputImageKey)
    var image = filter.outputImage!
    if resize { image = image.transformed(by: fit).composited(over: black) }
    if grain > 0 {  // a different patch of the noise field for every frame
        let patch = CGAffineTransform(translationX: -.random(in: 0...4000), y: -.random(in: 0...4000))
        let grains = noise.transformed(by: patch.concatenating(CGAffineTransform(scaleX: grainSize, y: grainSize)))
        image = speckle.apply(extent: canvas, arguments: [image, grains, grain * 0.1])!
    }
    CVBufferSetAttachment(dst, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
    context.render(image, to: dst, bounds: canvas, colorSpace: nil)
    filter.setValue(nil, forKey: kCIInputImageKey)
    if !adaptor.append(dst, withPresentationTime: time) { fail("\(writer.error!)") }
    frames += 1
    if frames % 30 == 0 {  // progress, in the format ffmpeg uses
        print("out_time_us=\(Int((time - from).seconds * 1e6))")
        fflush(stdout)
    }
    return true
}
while autoreleasepool(invoking: step) {}
if reader.status == .failed { fail("\(reader.error!)") }
guard frames > 0 else { fail("no frames in that range") }
input.markAsFinished()
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()
if writer.status != .completed { fail("\(writer.error!)") }
