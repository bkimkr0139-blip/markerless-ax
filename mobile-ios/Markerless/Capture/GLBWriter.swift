// GLBWriter.swift — 스캔 메시 → 최소 binary glTF (spatial_core.registration.glb.write_glb와 동일 포맷)
// POSITION(VEC3/float32) + indices(uint32)만 담는다. 좌표: 자산 로컬 프레임, 미터, Y-up(ARKit 월드 축)

import Foundation

enum GLBWriter {
    /// positions: 월드(또는 센터링된 자산 로컬) 좌표의 정점. indices: 삼각형 3개 조합.
    static func write(positions: [SIMD3<Float>], indices: [UInt32], name: String = "scan") -> Data {
        var pos = Data(capacity: positions.count * 12)
        // 주의: SIMD3<Float>는 메모리에서 16바이트(패딩 포함)다. 통째로 쓰면 glTF VEC3(12바이트)와 어긋나
        // 서버가 읽을 때 모든 좌표가 뒤섞인다 → float 3개를 하나씩 쓴다.
        for p in positions {
            for f in [p.x, p.y, p.z] {
                var bits = f.bitPattern.littleEndian
                withUnsafeBytes(of: &bits) { pos.append(contentsOf: $0) }
            }
        }
        var idx = Data(capacity: indices.count * 4)
        for i in indices {
            var v = i.littleEndian
            withUnsafeBytes(of: &v) { idx.append(contentsOf: $0) }
        }

        func pad(_ d: Int) -> Int { (4 - d % 4) % 4 }
        let posLen = pos.count, idxLen = idx.count
        let idxOffset = posLen + pad(posLen)
        let binLen = idxOffset + idxLen + pad(idxLen)

        var minX = Float.greatestFiniteMagnitude, minY = minX, minZ = minX
        var maxX = -Float.greatestFiniteMagnitude, maxY = maxX, maxZ = maxX
        for p in positions {
            minX = min(minX, p.x); maxX = max(maxX, p.x)
            minY = min(minY, p.y); maxY = max(maxY, p.y)
            minZ = min(minZ, p.z); maxZ = max(maxZ, p.z)
        }

        // json.dumps(separators)와 동일한 밀집 JSON — 파서 호환 목적
        let gltf = """
        {"asset":{"version":"2.0","generator":"markerless-poc-ios"},"scene":0,"scenes":[{"nodes":[0]}],\
        "nodes":[{"mesh":0,"name":"\(name)"}],"meshes":[{"name":"\(name)","primitives":[\
        {"attributes":{"POSITION":0},"indices":1,"mode":4}]}],\
        "buffers":[{"byteLength":\(binLen)}],\
        "bufferViews":[{"buffer":0,"byteOffset":0,"byteLength":\(posLen),"target":34962},\
        {"buffer":0,"byteOffset":\(idxOffset),"byteLength":\(idxLen),"target":34963}],\
        "accessors":[{"bufferView":0,"componentType":5126,"count":\(positions.count),"type":"VEC3",\
        "min":[\(minX),\(minY),\(minZ)],"max":[\(maxX),\(maxY),\(maxZ)]},\
        {"bufferView":1,"componentType":5125,"count":\(indices.count),"type":"SCALAR"}]}
        """
        var json = Data(gltf.utf8)
        json.append(contentsOf: repeatElement(0x20, count: pad(json.count)))  // 스페이스 패딩

        var out = Data()
        func u32(_ v: UInt32) { var x = v.littleEndian; withUnsafeBytes(of: &x) { out.append(contentsOf: $0) } }
        u32(0x46546C67); u32(2); u32(UInt32(12 + 8 + json.count + 8 + binLen))
        u32(UInt32(json.count)); u32(0x4E4F534A)
        out.append(json)
        u32(UInt32(binLen)); u32(0x004E4942)
        out.append(pos)
        out.append(contentsOf: repeatElement(0, count: pad(posLen)))
        out.append(idx)
        out.append(contentsOf: repeatElement(0, count: binLen - idxOffset - idxLen))
        return out
    }
}
