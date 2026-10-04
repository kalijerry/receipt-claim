// Turn a receipt photo into a clean "scanned" grayscale image.
// usage: scan <in> <out.jpg> <crop|box|enhance>
//   crop    = detect the paper, perspective-correct, then enhance. If the paper edge would cut through
//             any text line, fall back to a straight rectangle around the paper + that text instead.
//   box     = straight rectangle around all text in the photo (fallback when crop loses content)
//   enhance = keep the full frame, only enhance
// prints JSON: {"ok":true,"cropped":bool,"how":"perspective|box|full","w":..,"h":..,"upscale":..}
import CoreImage
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

func fail(_ msg: String) -> Never {
    print("{\"ok\":false,\"error\":\"\(msg)\"}")
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 4 else { fail("usage: scan <in> <out> <crop|enhance>") }
let inURL = URL(fileURLWithPath: args[1]), outURL = URL(fileURLWithPath: args[2]), mode = args[3]

guard var img = CIImage(contentsOf: inURL, options: [.applyOrientationProperty: true]) else { fail("cannot read image") }
img = img.transformed(by: CGAffineTransform(translationX: -img.extent.origin.x, y: -img.extent.origin.y))
let full = img.extent
var cropped = false
var how = "full"

func textBoxes(_ image: CIImage) -> [CGRect] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .fast
    req.usesLanguageCorrection = false
    try? VNImageRequestHandler(ciImage: image, options: [:]).perform([req])
    let e = image.extent
    return (req.results ?? []).map { o in
        let b = o.boundingBox
        return CGRect(x: b.minX * e.width, y: b.minY * e.height, width: b.width * e.width, height: b.height * e.height)
    }
}

// convex polygon test (points in order)
func inside(_ p: CGPoint, _ q: [CGPoint]) -> Bool {
    var sign: CGFloat = 0
    for i in 0..<q.count {
        let a = q[i], b = q[(i + 1) % q.count]
        let c = (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x)
        if c != 0 {
            if sign == 0 { sign = c } else if (c > 0) != (sign > 0) { return false }
        }
    }
    return true
}

func boxCrop(_ rects: [CGRect]) {
    guard var u = rects.first else { return }
    for r in rects.dropFirst() { u = u.union(r) }
    let pad = max(full.width, full.height) * 0.03
    u = u.insetBy(dx: -pad, dy: -pad).intersection(full)
    if u.width * u.height < full.width * full.height * 0.92 {
        img = img.cropped(to: u).transformed(by: CGAffineTransform(translationX: -u.minX, y: -u.minY))
        cropped = true
        how = "box"
    }
}

let boxes = (mode == "crop" || mode == "box") ? textBoxes(img) : []

if mode == "box" {
    boxCrop(boxes)
}

if mode == "crop" {
    let req = VNDetectDocumentSegmentationRequest()
    let handler = VNImageRequestHandler(ciImage: img, options: [:])
    try? handler.perform([req])
    if let q = req.results?.first {
        func p(_ n: CGPoint) -> CGPoint { CGPoint(x: n.x * full.width, y: n.y * full.height) }
        var tl = p(q.topLeft), tr = p(q.topRight), bl = p(q.bottomLeft), br = p(q.bottomRight)
        // grow the quad a little so text touching the paper edge is never cut
        let cx = (tl.x + tr.x + bl.x + br.x) / 4, cy = (tl.y + tr.y + bl.y + br.y) / 4
        func grow(_ pt: CGPoint) -> CGPoint {
            CGPoint(x: min(max(cx + (pt.x - cx) * 1.03, 0), full.width), y: min(max(cy + (pt.y - cy) * 1.02, 0), full.height))
        }
        tl = grow(tl); tr = grow(tr); bl = grow(bl); br = grow(br)
        // shoelace area: ignore tiny detections (probably not the receipt)
        let pts = [tl, tr, br, bl]
        var area: CGFloat = 0
        for i in 0..<4 { let a = pts[i], b = pts[(i + 1) % 4]; area += a.x * b.y - b.x * a.y }
        // text lines belonging to this paper must lie completely inside it, otherwise the crop would cut them
        let related = boxes.filter { inside(CGPoint(x: $0.midX, y: $0.midY), pts) }
        let cut = related.contains { b in
            let t = b.insetBy(dx: b.height * 0.2, dy: b.height * 0.2)
            return ![CGPoint(x: t.minX, y: t.minY), CGPoint(x: t.maxX, y: t.minY),
                     CGPoint(x: t.minX, y: t.maxY), CGPoint(x: t.maxX, y: t.maxY)].allSatisfy { inside($0, pts) }
        }
        if cut && abs(area) / 2 > full.width * full.height * 0.08 {
            let qb = CGRect(x: pts.map(\.x).min()!, y: pts.map(\.y).min()!, width: 0, height: 0)
                .union(CGRect(x: pts.map(\.x).max()!, y: pts.map(\.y).max()!, width: 0, height: 0))
            boxCrop([qb] + related)
        } else if abs(area) / 2 > full.width * full.height * 0.08 && q.confidence > 0.5 {
            let f = CIFilter(name: "CIPerspectiveCorrection")!
            f.setValue(img, forKey: kCIInputImageKey)
            f.setValue(CIVector(cgPoint: tl), forKey: "inputTopLeft")
            f.setValue(CIVector(cgPoint: tr), forKey: "inputTopRight")
            f.setValue(CIVector(cgPoint: bl), forKey: "inputBottomLeft")
            f.setValue(CIVector(cgPoint: br), forKey: "inputBottomRight")
            if let out = f.outputImage {
                img = out.transformed(by: CGAffineTransform(translationX: -out.extent.origin.x, y: -out.extent.origin.y))
                cropped = true
                how = "perspective"
            }
        }
    }
}

// low-resolution sources (e.g. 768x1024 exports): upscale smoothly so print isn't blocky
var upscale: CGFloat = 1
if img.extent.width < 1100 {
    let k = min(3.0, 1100.0 / img.extent.width)
    upscale = k
    img = img.applyingFilter("CILanczosScaleTransform", parameters: [kCIInputScaleKey: k, kCIInputAspectRatioKey: 1.0])
    img = img.transformed(by: CGAffineTransform(translationX: -img.extent.origin.x, y: -img.extent.origin.y))
}

// --- enhance: grayscale, flatten uneven lighting, push paper to white, sharpen ---
let ext = img.extent
var g = img.applyingFilter("CIColorControls", parameters: [kCIInputSaturationKey: 0, kCIInputContrastKey: 1.0])
// background estimate: remove text with a max filter, then blur
let r = max(6, min(ext.width, ext.height) / 60)
let bg = g.applyingFilter("CIMorphologyMaximum", parameters: [kCIInputRadiusKey: r])
    .clampedToExtent()
    .applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: r * 2])
    .cropped(to: ext)
// gray / background -> evenly lit page
g = bg.applyingFilter("CIDivideBlendMode", parameters: [kCIInputBackgroundImageKey: g]).cropped(to: ext)
// levels: anything lighter than ~88% becomes paper white, darken ink
g = g.applyingFilter("CIColorMatrix", parameters: [
    "inputRVector": CIVector(x: 1.45, y: 0, z: 0, w: 0),
    "inputGVector": CIVector(x: 0, y: 1.45, z: 0, w: 0),
    "inputBVector": CIVector(x: 0, y: 0, z: 1.45, w: 0),
    "inputBiasVector": CIVector(x: -0.33, y: -0.33, z: -0.33, w: 0),
]).applyingFilter("CIColorClamp")
g = g.applyingFilter("CIGammaAdjust", parameters: ["inputPower": 1.35])
g = g.applyingFilter("CIUnsharpMask", parameters: [kCIInputRadiusKey: 1.6, kCIInputIntensityKey: 0.6]).cropped(to: ext)

let ctx = CIContext(options: [.workingColorSpace: CGColorSpaceCreateDeviceRGB()])
guard let cg = ctx.createCGImage(g, from: ext, format: .L8, colorSpace: CGColorSpaceCreateDeviceGray()) else { fail("render failed") }
guard let dest = CGImageDestinationCreateWithURL(outURL as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else { fail("cannot write") }
CGImageDestinationAddImage(dest, cg, [kCGImageDestinationLossyCompressionQuality: 0.9, kCGImagePropertyDPIWidth: 300, kCGImagePropertyDPIHeight: 300] as CFDictionary)
guard CGImageDestinationFinalize(dest) else { fail("cannot write") }
print("{\"ok\":true,\"cropped\":\(cropped),\"how\":\"\(how)\",\"w\":\(cg.width),\"h\":\(cg.height),\"upscale\":\(upscale)}")
