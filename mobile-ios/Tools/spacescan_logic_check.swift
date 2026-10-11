// 실행: tools 폴더에서  mkdir -p /tmp/ssl && cp spacescan_logic_check.swift /tmp/ssl/main.swift && swiftc -o /tmp/ssl/run ../Markerless/Capture/SpaceScanLogic.swift /tmp/ssl/main.swift && /tmp/ssl/run
import Foundation
import simd
var fails = 0
func check(_ c: Bool, _ m: String) { if !c { fails += 1; print("FAIL", m) } }
func T(_ x: Float, _ y: Float, _ z: Float, yawDeg: Float = 0) -> simd_float4x4 {
    var m = simd_float4x4(simd_quatf(angle: yawDeg * .pi / 180, axis: [0, 1, 0])); m.columns.3 = [x, y, z, 1]; return m
}
// chunk planner
check(ChunkPlanner.plan(total: 2_500_000, received: 0).count == 3, "chunks 3")
check(ChunkPlanner.plan(total: 2_500_000, received: 1 << 20).first?.offset == 1 << 20, "resume offset")
check(ChunkPlanner.plan(total: 100, received: 100).isEmpty, "done")
check(ChunkPlanner.plan(total: 0, received: 0).isEmpty, "empty")
// gate
var g = KeyframeGate()
_ = g.observe(transform: T(0, 0, 0), trackingNormal: true)
check(g.decide(transform: T(0, 0, 0), trackingNormal: true, blur: 0.1) == .accept, "first accept")
check(g.decide(transform: T(0.05, 0, 0), trackingNormal: true, blur: 0.1) == .skipNotMoved, "not moved")
check(g.decide(transform: T(0.2, 0, 0), trackingNormal: true, blur: 0.1) == .accept, "moved 0.2")
check(g.decide(transform: T(0.2, 0, 0, yawDeg: 12), trackingNormal: true, blur: 0.1) == .accept, "turned 12deg")
check(g.decide(transform: T(0.2, 0, 0, yawDeg: 15), trackingNormal: true, blur: 0.1) == .skipNotMoved, "turned only 3deg more")
check(g.decide(transform: T(5, 0, 0), trackingNormal: false, blur: 0.1) == .skipTracking, "tracking")
check(g.decide(transform: T(5, 0, 0), trackingNormal: true, blur: 0.9) == .skipBlur, "blur")
var g2 = KeyframeGate()
check(!g2.observe(transform: T(0, 0, 0), trackingNormal: true), "no jump")
check(!g2.observe(transform: T(0.02, 0, 0), trackingNormal: true), "no jump small")
check(g2.observe(transform: T(1.0, 0, 0), trackingNormal: true) && g2.segmentIndex == 1, "jump -> segment 1")
_ = g2.observe(transform: T(1, 0, 0), trackingNormal: false)
check(g2.observe(transform: T(1, 0, 0), trackingNormal: true) && g2.segmentIndex == 2, "relocalize -> segment 2")
check(KeyframeGate.blurScore(angularRadPerS: 0.1, linearMPerS: 0.1, exposureS: 0.01, fxPx: 1500) < 0.5, "slow = low blur")
check(KeyframeGate.blurScore(angularRadPerS: 1.5, linearMPerS: 0.1, exposureS: 0.02, fxPx: 1500) > 0.5, "fast = high blur")
// coverage
var c = SpaceCoverageGrid()
check(SpaceCoverageGrid.cell(forward: [0, 1, 0]).band == 2, "ceiling")
check(SpaceCoverageGrid.cell(forward: [0, -1, 0]).band == 0, "floor")
check(SpaceCoverageGrid.cell(forward: [0, 0, -1]) == (1, 0), "-Z sector0")
check(SpaceCoverageGrid.cell(forward: [1, 0, 0]) == (1, 6), "+X sector 6")
for s in 0..<24 { let a = (Float(s) + 0.5) * 15 * .pi / 180; for _ in 0..<2 { c.add(forward: [sin(a), 0, -cos(a)]) } }
check(c.wallFraction == 1, "walls full")
check(c.hint(headingRad: 0).contains("천장"), "hint ceiling: \(c.hint(headingRad: 0))")
var c2 = SpaceCoverageGrid()
for s in 0..<24 where ![2,3,4].contains(s) { let a = (Float(s) + 0.5) * 15 * .pi / 180; for _ in 0..<2 { c2.add(forward: [sin(a), 0, -cos(a)]) } }
print(c2.hint(headingRad: 0), "|", c2.hint(headingRad: 3.0), "|", c2.hint(headingRad: 0.7))
check(c2.hint(headingRad: 0).contains("오른쪽"), "right corner")

// ── sharpness
func gen(_ w: Int, _ h: Int, _ f: (Int, Int) -> UInt8) -> [UInt8] { var a = [UInt8](repeating: 0, count: w*h); for y in 0..<h { for x in 0..<w { a[y*w+x] = f(x,y) } }; return a }
let sharpImg = gen(64, 48) { x, y in ((x / 2 + y / 2) % 2 == 0) ? 220 : 30 }
var blurImg = sharpImg
for _ in 0..<3 { var o = blurImg; for y in 1..<47 { for x in 1..<63 { var s = 0; for dy in -1...1 { for dx in -1...1 { s += Int(blurImg[(y+dy)*64+x+dx]) } }; o[y*64+x] = UInt8(s/9) } }; blurImg = o }
let vs = SharpnessMath.laplacianVariance(luma: sharpImg, width: 64, height: 48)
let vb = SharpnessMath.laplacianVariance(luma: blurImg, width: 64, height: 48)
check(vs > 5 * vb && vb >= 0, "laplacian sharp \(vs) >> blur \(vb)")
check(SharpnessMath.laplacianVariance(luma: [UInt8](repeating: 100, count: 64*48), width: 64, height: 48) == 0, "flat = 0")
check(SharpnessMath.sharpBlur(sharpness: 100, reference: 100) == 0, "ref sharp -> 0")
check(SharpnessMath.sharpBlur(sharpness: 10, reference: 100) == 1, "10% of ref -> 1")
check(SharpnessMath.sharpBlur(sharpness: 5, reference: 8) == 0, "small ref -> no judgement")
check(SharpnessMath.sharpBlur(sharpness: 5, reference: nil) == 0, "nil ref -> 0")
check(SharpnessMath.combine(speedBlur: 0.1, sharpBlur: 0.6) > 0.6 && SharpnessMath.combine(speedBlur: 0.9, sharpBlur: 0.9) == 1, "combine")
check(SharpnessMath.combine(speedBlur: 0, sharpBlur: 0) == 0, "combine zero")
var ref: Float? = nil
ref = SharpnessMath.updateReference(ref, windowBest: 100)
for _ in 0..<6 { ref = SharpnessMath.updateReference(ref, windowBest: 20) }
check(SharpnessMath.sharpBlur(sharpness: 20, reference: ref) < 0.5, "reference adapts to bland scene: \(ref!)")
// ── picker
var pk = BestFramePicker<Int>()
var got: (item: Int, score: Float, t: Double)? = nil
for (i, sc) in [0.4, 0.3, 0.1, 0.2, 0.35, 0.05, 0.01].enumerated() as EnumeratedSequence<[Double]> where got == nil {
    got = pk.offer(i, score: Float(sc), at: Double(i) * 0.016)
}
check(got?.item == 5 && !pk.isOpen, "picker: 6 frames -> best idx5 (\(String(describing: got?.item)))")
var pk2 = BestFramePicker<Int>()
check(pk2.offer(0, score: 0.3, at: 0) == nil, "pk2 open")
check(pk2.offer(1, score: 0.2, at: 0.2) == nil, "pk2 still open")
let r2 = pk2.offer(2, score: 0.25, at: 0.45)
check(r2?.item == 1, "picker: time limit -> best idx1")
var pk3 = BestFramePicker<Int>(); pk3.maxFrames = 2
_ = pk3.offer(0, score: 0.2, at: 0); check(pk3.offer(1, score: 0.2, at: 0.01)?.item == 0, "tie -> earlier")
// gate window API
var g3 = KeyframeGate()
check(g3.motionOpen(transform: T(0,0,0)), "open first")
g3.commit(transform: T(0,0,0))
check(!g3.motionOpen(transform: T(0.05,0,0)) && g3.motionOpen(transform: T(0.2,0,0)), "motionOpen after commit")
// ── depth denoise
struct LCG { var s: UInt64; mutating func u() -> Double { s = s &* 6364136223846793005 &+ 1442695040888963407; return Double((s >> 11) & ((1<<53)-1)) / Double(1<<53) }
  mutating func g() -> Float { let a = max(1e-12, u()), b = u(); return Float((-2*log(a)).squareRoot() * cos(2 * .pi * b)) } }
var rng = LCG(s: 42)
let px = 20000, sigma: Float = 0.01
var singles: [[Float]] = [], cf: [[UInt8]] = []
for _ in 0..<5 { singles.append((0..<px).map { _ in 2.0 + sigma * rng.g() }); cf.append([UInt8](repeating: 2, count: px)) }
func std(_ a: [Float]) -> Float { let m = a.reduce(0, +) / Float(a.count); return (a.map { ($0-m)*($0-m) }.reduce(0, +) / Float(a.count)).squareRoot() }
let dn = DepthDenoise.average(depths: singles, confs: cf)
let ratio = std(dn.depth) / std(singles[0])
check(ratio > 0.40 && ratio < 0.50, "std ratio ~1/sqrt5=0.447, got \(ratio)")
check(dn.averagedPixels == px && dn.conf.allSatisfy { $0 == 2 }, "all averaged, conf 2")
// invalid / single / outlier
let dA: [[Float]] = [[1, 0, 0, 2, 3], [1.01, .nan, 0, 2, 3], [0.99, 0, 0, 2.5, 3]]
let cA: [[UInt8]] = [[2,2,2,2,0], [2,2,2,2,2], [2,2,2,2,1]]
let rA = DepthDenoise.average(depths: dA, confs: cA)
check(abs(rA.depth[0] - 1.0) < 0.01, "avg px0")
check(rA.depth[1] == 0 && rA.conf[1] == 2 || rA.depth[1] == 0, "all invalid -> 0")
check(rA.depth[2] == 0, "zero stays zero")
check(abs(rA.depth[3] - 2.0) < 1e-5, "outlier 2.5 rejected -> 2.0 (\(rA.depth[3]))")
check(rA.conf[4] == 1 && abs(rA.depth[4] - 3) < 1e-5, "mean conf (0,2,1)->1")
let rS = DepthDenoise.average(depths: [[0, 1.5], [2.0, 0]], confs: [[2,1],[1,2]])
check(rS.depth == [2.0, 1.5] && rS.conf == [1, 1] && rS.averagedPixels == 0, "single valid sample kept")
let wD = DepthDenoise.average(depths: [[2.0], [2.02]], confs: [[2],[0]])
check(abs(wD.depth[0] - (2.0*2 + 2.02*0.25)/2.25) < 1e-5, "conf weighting")
var ringT = DepthRing(); for i in 0..<7 { ringT.push(depth: [Float(i)], conf: [1]) }
check(ringT.count == 5 && ringT.depths.first! == [2], "ring keeps last 5")
check(abs(DepthDenoise.meanConfidence(depth: [1, 0, 2], conf: [2, 2, 0]) - 1) < 1e-6, "mean confidence over valid")
// ── still capture
let ki = StillCapture.scaleIntrinsics(fx: 1500, fy: 1500, cx: 960, cy: 720, fromW: 1920, fromH: 1440, toW: 4032, toH: 3024)
check(abs(ki[0] - 3150) < 1e-3 && abs(ki[4] - 3150) < 1e-3 && abs(ki[6] - 2016) < 1e-3 && abs(ki[7] - 1512) < 1e-3 && ki[8] == 1, "intrinsics scale (principal point stays at image center)")
check(StillCapture.poseCompatible(T(0,0,0), T(0.005,0,0)) && !StillCapture.poseCompatible(T(0,0,0), T(0.02,0,0)) && !StillCapture.poseCompatible(T(0,0,0), T(0,0,0, yawDeg: 1)), "pose compat")
print(fails == 0 ? "ALL OK" : "\(fails) FAILED")
