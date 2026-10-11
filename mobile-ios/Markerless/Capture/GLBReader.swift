// GLBReader.swift — 백엔드 자산 GLB 파서 → RealityKit MeshDescriptor용 정점/인덱스
// PoC 서브셋: 첫 primitive의 POSITION(VF3) + indices(U8/U16/U32). spatial_core 리더와 동일 범위.

import Foundation
import simd

struct GLBMesh {
    var positions: [SIMD3<Float>]
    var indices: [UInt32]
}

enum GLBError: LocalizedError {
    case notGLB, noMesh
    var errorDescription: String? {
        switch self {
        case .notGLB: return "GLB 형식이 아닙니다"
        case .noMesh: return "GLB에 메시가 없습니다"
        }
    }
}

enum GLBReader {
    static func read(_ data: Data) throws -> GLBMesh {
        guard data.count > 20 else { throw GLBError.notGLB }
        let magic = data.u32le(0)
        guard magic == 0x46546C67, data.u32le(4) == 2 else { throw GLBError.notGLB }

        // 청크 순회: JSON + BIN
        var jsonDoc: [String: Any] = [:]
        var bin: Data = Data()
        var off = 12
        while off + 8 <= data.count {
            let clen = Int(data.u32le(off))
            let ctype = data.u32le(off + 4)
            let start = off + 8
            guard start + clen <= data.count else { break }
            if ctype == 0x4E4F534A, let obj = try? JSONSerialization.jsonObject(with: data[start..<start+clen]) as? [String: Any] {
                jsonDoc = obj
            } else if ctype == 0x004E4942 {
                bin = data.subdata(in: start..<start+clen)
            }
            off = start + clen + ((4 - clen % 4) % 4)
        }

        guard
            let meshes = jsonDoc["meshes"] as? [[String: Any]],
            let prim = (meshes.first?["primitives"] as? [[String: Any]])?.first,
            let accessors = jsonDoc["accessors"] as? [[String: Any]],
            let bufferViews = jsonDoc["bufferViews"] as? [[String: Any]],
            let attrs = prim["attributes"] as? [String: Any],
            let posIdx = attrs["POSITION"] as? Int
        else { throw GLBError.noMesh }

        let positions = try readVec3(accessorIndex: posIdx, accessors: accessors, bufferViews: bufferViews, bin: bin)
        var indices: [UInt32] = []
        if let idxAccessor = prim["indices"] as? Int {
            indices = try readIndices(accessorIndex: idxAccessor, accessors: accessors, bufferViews: bufferViews, bin: bin)
        } else {
            indices = Array(0..<UInt32(positions.count))
        }
        return GLBMesh(positions: positions, indices: indices)
    }

    // MARK: - accessors

    private static func viewBounds(_ accessor: [String: Any], _ bufferViews: [[String: Any]],
                                   _ bin: Data) throws -> (Data, Int) {
        guard let bvi = accessor["bufferView"] as? Int else { throw GLBError.notGLB }
        let bv = bufferViews[bvi]
        let start = bv["byteOffset"] as? Int ?? 0
        let len = bv["byteLength"] as? Int ?? 0
        guard start + len <= bin.count else { throw GLBError.notGLB }
        return (bin, start + (accessor["byteOffset"] as? Int ?? 0))
    }

    private static func readVec3(accessorIndex: Int, accessors: [[String: Any]],
                                 bufferViews: [[String: Any]], bin: Data) throws -> [SIMD3<Float>] {
        let acc = accessors[accessorIndex]
        guard acc["type"] as? String == "VEC3", acc["componentType"] as? Int == 5126 else { throw GLBError.notGLB }
        let (binData, offset) = try viewBounds(acc, bufferViews, bin)
        let count = acc["count"] as? Int ?? 0
        // SIMD3<Float>는 16바이트 간격이라 바이트 복사로는 안 된다 — 12바이트(float 3개)씩 읽는다
        var out = [SIMD3<Float>](repeating: .zero, count: count)
        binData.withUnsafeBytes { raw in
            for i in 0..<count {
                let o = offset + i * 12
                out[i] = SIMD3<Float>(raw.loadUnaligned(fromByteOffset: o, as: Float.self),
                                      raw.loadUnaligned(fromByteOffset: o + 4, as: Float.self),
                                      raw.loadUnaligned(fromByteOffset: o + 8, as: Float.self))
            }
        }
        return out
    }

    private static func readIndices(accessorIndex: Int, accessors: [[String: Any]],
                                    bufferViews: [[String: Any]], bin: Data) throws -> [UInt32] {
        let acc = accessors[accessorIndex]
        let (binData, offset) = try viewBounds(acc, bufferViews, bin)
        let count = acc["count"] as? Int ?? 0
        switch acc["componentType"] as? Int {
        case 5121: // u8
            let sub = binData.subdata(in: offset..<offset + count)
            return sub.map { UInt32($0) }
        case 5123: // u16
            let sub = binData.subdata(in: offset..<offset + count * 2)
            return (0..<count).map { UInt32(sub.u16le($0 * 2)) }
        case 5125: // u32
            let sub = binData.subdata(in: offset..<offset + count * 4)
            return (0..<count).map { sub.u32le($0 * 4) }
        default:
            throw GLBError.notGLB
        }
    }
}

private extension Data {
    /// little-endian u32 — 바이트 직접 합성 (중첩 unsafe 클로저 회피)
    func u32le(_ offset: Int) -> UInt32 {
        let i = startIndex + offset
        return UInt32(self[i])
            | UInt32(self[i + 1]) << 8
            | UInt32(self[i + 2]) << 16
            | UInt32(self[i + 3]) << 24
    }

    /// little-endian u16
    func u16le(_ offset: Int) -> UInt16 {
        let i = startIndex + offset
        return UInt16(self[i]) | UInt16(self[i + 1]) << 8
    }
}
