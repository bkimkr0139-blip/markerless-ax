// Keyframe.swift — 스캔 중 저장하는 카메라 키프레임 (등록 때 방향별 사진 실루엣의 원본)

import CoreImage
import simd

struct Keyframe {
    let jpeg: Data
    let transform: simd_float4x4  // 카메라→세션 월드
    let fx: Float, fy: Float, cx: Float, cy: Float  // JPEG 크기에 맞춘 내부 파라미터 (좌상단 원점)
    let width: Int, height: Int

    var position: SIMD3<Float> { SIMD3(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z) }
    /// 카메라가 바라보는 방향 (−Z)
    var forward: SIMD3<Float> {
        simd_normalize(-SIMD3(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z))
    }

    private static let ciContext = CIContext()

    /// 픽셀버퍼를 maxWidth로 줄여 JPEG로 인코딩. (data, 줄인 폭, 줄인 높이, 배율)
    nonisolated static func encode(_ pb: CVPixelBuffer, maxWidth: Int = 960) -> (Data, Int, Int, Float)? {
        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
        let s = min(1.0, Float(maxWidth) / Float(w))
        let ci = CIImage(cvPixelBuffer: pb).transformed(by: CGAffineTransform(scaleX: CGFloat(s), y: CGFloat(s)))
        guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
              let data = ciContext.jpegRepresentation(
                of: ci, colorSpace: cs,
                options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.75])
        else { return nil }
        return (data, Int((Float(w) * s).rounded()), Int((Float(h) * s).rounded()), s)
    }
}

/// CVPixelBuffer는 Sendable이 아니라 백그라운드 인코딩으로 넘길 때 감싼다 (읽기만 한다)
struct PixelBufferBox: @unchecked Sendable {
    let buffer: CVPixelBuffer
}
