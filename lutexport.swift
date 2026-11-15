// GPU video export for app.py: applies a .cube LUT to a stretch of video and encodes it as
// 10-bit HEVC, with decoding, the LUT and encoding all on Apple hardware. Video only; app.py
// adds the audio with ffmpeg. Built on demand by app.py:
//   swiftc -O -swift-version 5 lutexport.swift -o lutexport
//   lutexport input start duration lut.cube output.mov width height
import AVFoundation
import CoreImage
import VideoToolbox

func fail(_ msg: String) -> Never { FileHandle.standardError.write((msg + "\n").data(using: .utf8)!); exit(1) }

let a = CommandLine.arguments
guard a.count == 8, let start = Double(a[2]), let length = Double(a[3]), let w = Int(a[6]), let h = Int(a[7])
else { fail("usage: lutexport input start duration lut.cube output.mov width height") }

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
// Each frame runs in its own autorelease pool. Without one, frames are never handed back to
// the decoder, which then fails with "Cannot Decode" after about 77 seconds of video.
func step() -> Bool {
    guard let sample = output.copyNextSampleBuffer() else { return false }
    guard let src = CMSampleBufferGetImageBuffer(sample) else { return true }
    let time = CMSampleBufferGetPresentationTimeStamp(sample)
    if time < from || time >= to { return true }  // the same frames ffmpeg's -ss / -t would keep
    if frames == 0 { writer.startSession(atSourceTime: time) }
    while !input.isReadyForMoreMediaData { usleep(500) }
    var made: CVPixelBuffer?
    CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &made)
    guard let dst = made else { fail("out of video memory") }
    filter.setValue(CIImage(cvPixelBuffer: src, options: [.colorSpace: none]).oriented(orientation), forKey: kCIInputImageKey)
    var image = filter.outputImage!
    if resize { image = image.transformed(by: fit).composited(over: black) }
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
