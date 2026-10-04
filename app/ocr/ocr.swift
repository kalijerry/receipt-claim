// macOS Vision OCR: ocr <image>... -> JSON lines {"file":..,"lines":[{"text":..,"conf":..,"box":[x,y,w,h]}]}
import Foundation
import Vision
import ImageIO

func exifDate(_ path: String) -> String? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
          let exif = props[kCGImagePropertyExifDictionary] as? [CFString: Any] else { return nil }
    return exif[kCGImagePropertyExifDateTimeOriginal] as? String
}

func ocr(_ path: String) -> [[String: Any]] {
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return [] }
    var orient = CGImagePropertyOrientation.up
    if let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
       let o = props[kCGImagePropertyOrientation] as? UInt32,
       let po = CGImagePropertyOrientation(rawValue: o) { orient = po }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["en-GB", "zh-Hans"]
    let handler = VNImageRequestHandler(cgImage: img, orientation: orient, options: [:])
    try? handler.perform([req])
    var out: [[String: Any]] = []
    for obs in req.results ?? [] {
        guard let c = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox
        out.append(["text": c.string, "conf": c.confidence,
                    "box": [b.origin.x, 1 - b.origin.y - b.height, b.width, b.height]])
    }
    // top-to-bottom, then left-to-right
    out.sort { a, b in
        let ba = a["box"] as! [CGFloat], bb = b["box"] as! [CGFloat]
        let ya = ba[1] + ba[3] / 2, yb = bb[1] + bb[3] / 2
        if abs(ya - yb) > min(ba[3], bb[3]) * 0.5 { return ya < yb }
        return ba[0] < bb[0]
    }
    return out
}

for p in CommandLine.arguments.dropFirst() {
    var obj: [String: Any] = ["file": p, "lines": ocr(p)]
    if let d = exifDate(p) { obj["exif_date"] = d }
    let d = try! JSONSerialization.data(withJSONObject: obj)
    print(String(data: d, encoding: .utf8)!)
}
