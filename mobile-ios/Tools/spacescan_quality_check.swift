// 실행: mobile-ios 폴더에서
//  mkdir -p /tmp/ssq && cp Tools/spacescan_quality_check.swift /tmp/ssq/main.swift && swiftc -o /tmp/ssq/run Markerless/Capture/ScanQualityKit.swift Markerless/Capture/SpaceScanLogic.swift Markerless/Capture/SpaceQualityLogic.swift /tmp/ssq/main.swift && /tmp/ssq/run
import Foundation
import simd
var fails = 0
func check(_ c: Bool, _ m: String) { if !c { fails += 1; print("FAIL:", m) } }
func fwd(az: Float, pitch: Float = 0) -> SIMD3<Float> {  // az: 0 = -Z, 시계방향(+X 쪽) / pitch: 위 +
    let a = az * .pi / 180, p = pitch * .pi / 180
    return [sin(a) * cos(p), sin(p), -cos(a) * cos(p)]
}

// ── grid ↔ heading mapping
check(SpaceGridMap.cell(forward: fwd(az: 0))! .sector == 0, "az0 -> sector0")
check(SpaceGridMap.cell(forward: fwd(az: 90))!.sector == 6 && SpaceGridMap.cell(forward: fwd(az: 90))!.ring == 1, "az90 -> sector 6 wall")
check(SpaceGridMap.cell(forward: fwd(az: 359))!.sector == 23, "az359 -> 23")
check(SpaceGridMap.cell(forward: fwd(az: 10, pitch: 40))!.ring == 2, "pitch 40 ceiling")
check(SpaceGridMap.cell(forward: fwd(az: 10, pitch: -40))!.ring == 0, "pitch -40 floor")
check(SpaceGridMap.cell(forward: fwd(az: 10, pitch: 25))!.ring == 1, "pitch 25 wall")
check(SpaceGridMap.cell(forward: [0, 0, 0]) == nil && SpaceGridMap.cell(forward: [.nan, 0, 1]) == nil, "bad fwd")
for deg in stride(from: 3, to: 360, by: 17) {
    let a = Float(deg) * .pi / 180
    check(SpaceGridMap.sector(headingRad: a) == SpaceGridMap.cell(forward: fwd(az: Float(deg)))!.sector, "heading sector == fwd sector @\(deg)")
}
check(SpaceGridMap.sector(headingRad: -0.1) == 23, "negative heading wraps")

// ── FOV neighbour filling
check(SpaceGridMap.neighborOffsets(fovDeg: 62, pitchDeg: 0) == 1, "wall: 18.6deg -> +-1 sector")
check(SpaceGridMap.neighborOffsets(fovDeg: 62, pitchDeg: 55) >= 2, "ceiling: wider span")
check(SpaceGridMap.neighborOffsets(fovDeg: 62, pitchDeg: 89) <= 11, "capped")
var g = SpaceGridMap.newGrid()
check(g.rings == 3 && g.sectors == 24 && g.need == 3, "grid dims")
let hits = SpaceGridMap.add(to: &g, forward: fwd(az: 7.5), quality: 0.8)   // sector 0 center
check(hits.count == 3, "centre + 2 neighbours (\(hits.count))")
check(g.cell(1, 0).frames == 1 && g.cell(1, 1).frames == 1 && g.cell(1, 23).frames == 1 && g.cell(1, 2).frames == 0, "wraps sector 23/0/1")
check(abs(g.cell(1, 0).avg - 0.8) < 1e-5 && abs(g.cell(1, 1).avg - 0.68) < 1e-5, "neighbour weight 0.85")
check(g.cell(0, 0).frames == 0 && g.cell(2, 0).frames == 0, "other rings untouched")
for _ in 0..<2 { SpaceGridMap.add(to: &g, forward: fwd(az: 7.5), quality: 0.8) }
check(g.state(1, 0) >= .ok && g.state(1, 1) >= .ok && g.state(1, 23) >= .ok, "one pan fills 3 cells")
// low quality sample doesn't make ok
var gl = SpaceGridMap.newGrid()
for _ in 0..<5 { SpaceGridMap.add(to: &gl, forward: fwd(az: 100), quality: 0.2) }
check(gl.state(1, 6) == .low, "low quality stays low")
check(SpaceGridMap.add(to: &gl, forward: fwd(az: 100), quality: .nan).isEmpty, "nan quality ignored")
// transitions reported
var gt = SpaceGridMap.newGrid(); var upgrades = 0
for _ in 0..<4 { for h in SpaceGridMap.add(to: &gt, forward: fwd(az: 200), quality: 0.9) where h.old < .ok && h.new >= .ok { upgrades += 1 } }
check(upgrades == 3, "3 cells crossed ok exactly once (\(upgrades))")

// ── sample log rollback (구간 제외)
var gr = SpaceGridMap.newGrid(); var log = SampleLog()
for i in 0..<20 { let t = Double(i) * 0.1; log.append(t: t, hits: SpaceGridMap.add(to: &gr, forward: fwd(az: 50), quality: 0.9)) }
for i in 0..<10 { let t = 2.0 + Double(i) * 0.1; log.append(t: t, hits: SpaceGridMap.add(to: &gr, forward: fwd(az: 250), quality: 0.9)) }
let before = gr.cell(1, 16).frames
check(before == 10, "second pan 10 samples")
let n = log.rollback(since: 2.0, grid: &gr)
check(n == 10 && gr.cell(1, 16).frames == 0 && gr.cell(1, 16).sumQ == 0 && gr.cell(1, 3).frames == 20, "rollback removes only recent (\(n))")

// ── depth centre-window stats
let W = 256, H = 192
var depth = [Float](repeating: 9.0, count: W * H), conf = [UInt8](repeating: 0, count: W * H)
for y in 0..<H { for x in 0..<W {
    let inC = x >= 77 && x < 179 && y >= 58 && y < 134
    if inC { depth[y * W + x] = 2.0; conf[y * W + x] = 2 }
} }
var r = depth.withUnsafeBufferPointer { d in conf.withUnsafeBufferPointer { c in
    DepthWindowStats.compute(width: W, height: H, depth: d.baseAddress!, depthStride: W, conf: c.baseAddress!, confStride: W) } }
check(r.medianM == 2.0 && r.validRatio == 1 && (r.meanConf ?? 0) > 1.9, "centre window ignores outer 9m: \(r)")
check(r.samples > 1500 && r.samples < 2800, "stride sampled count \(r.samples)")
// holes: 0 and NaN
var d2 = depth
for y in stride(from: 0, to: H, by: 3) { for x in 0..<W { d2[y * W + x] = (x % 2 == 0) ? 0 : .nan } }
r = d2.withUnsafeBufferPointer { d in conf.withUnsafeBufferPointer { c in
    DepthWindowStats.compute(width: W, height: H, depth: d.baseAddress!, depthStride: W, conf: c.baseAddress!, confStride: W) } }
check(r.medianM == 2.0 && r.validRatio < 0.95 && r.validRatio > 0.3, "invalid pixels excluded, ratio \(r.validRatio)")
// all invalid
let dz = [Float](repeating: 0, count: W * H)
r = dz.withUnsafeBufferPointer { d in DepthWindowStats.compute(width: W, height: H, depth: d.baseAddress!, depthStride: W, conf: nil, confStride: 0) }
check(r.medianM == nil && r.validRatio == 0 && r.meanConf == nil, "all invalid")
// median robust to outliers + padded stride
let pad = 300; var dp = [Float](repeating: 3.0, count: pad * H)
for i in 0..<(pad * H) where i % 17 == 0 { dp[i] = 0.05 }
r = dp.withUnsafeBufferPointer { d in DepthWindowStats.compute(width: W, height: H, depth: d.baseAddress!, depthStride: pad, conf: nil, confStride: 0) }
check(r.medianM == 3.0 && r.meanConf == nil, "median robust, padded stride")
// feeds LiveQuality
var inp = LiveQuality.Input(); inp.distanceM = 2.0; inp.depthConf = 2; inp.depthValid = 1
check(LiveQuality.evaluate(inp, profile: .space).level == .good, "good input -> good")
inp.distanceM = 0.2; check(LiveQuality.evaluate(inp, profile: .space).reasons.first == .tooClose, "too close")

// ── problems -> review items
var gp = SpaceGridMap.newGrid()
for s in 0..<24 { for _ in 0..<4 { gp.add(ring: 1, sector: s, quality: 0.9); gp.add(ring: 0, sector: s, quality: 0.9); gp.add(ring: 2, sector: s, quality: 0.9) } }
check(SpaceReview.items(gp, startHeadingRad: 0).isEmpty && !SpaceReview.needsReview(gp), "complete grid -> no review")
// 점검 트리거: 바닥·천장의 작은 구멍만 있으면 자동으로 띄우지 않고, 벽이 비거나 링이 절반 미만이면 띄운다
var gPin = QualityGrid(rings: 3, sectors: 24, need: 1)
for r in 0..<3 { for s in 0..<24 { gPin.add(ring: r, sector: s, quality: 0.9) } }
gPin.clear(ring: 2, sector: 5); gPin.clear(ring: 0, sector: 9)  // 천장·바닥 한 칸씩만 비어 있음
check(!gPin.problems().isEmpty && !SpaceReview.needsReview(gPin), "floor/ceiling pinholes alone must not nag")
gPin.clear(ring: 1, sector: 3)
check(SpaceReview.needsReview(gPin), "a wall hole must trigger review")
var gHalfRing = QualityGrid(rings: 3, sectors: 24, need: 1)
for s in 0..<24 { gHalfRing.add(ring: 1, sector: s, quality: 0.9) }
for s in 0..<8 { gHalfRing.add(ring: 2, sector: s, quality: 0.9) }  // 천장 33%, 바닥 0% → 링 절반 미만
check(SpaceReview.needsReview(gHalfRing), "ring under 50% must trigger review")
for s in 6...9 { gp.clear(ring: 1, sector: s) }             // 벽 7~10 비어 있음
for s in [20, 21] { gp.clear(ring: 1, sector: s) }           // 벽 21~22 비어 있음
for s in 3...3 { gp.clear(ring: 2, sector: s); gp.add(ring: 2, sector: s, quality: 0.2) }  // 천장 4 낮음
let items = SpaceReview.items(gp, startHeadingRad: 0)
check(items.count == 3, "3 items \(items.count)")
check(items[0].title == "벽 구간 7~10 · 비어 있음", "title0 \(items[0].title)")
check(items[1].title == "벽 구간 21~22 · 비어 있음", "title1 \(items[1].title)")
check(items[2].severity == .low && items[2].title == "천장 구간 4 · 품질 낮음" && items[2].detail.contains("위쪽"), "low last \(items[2].title) \(items[2].detail)")
check(items[0].detail == "시작 방향에서 오른쪽 120° 부근", "dir \(items[0].detail)")   // center = (6+2)*15 = 120
check(items[0].sector == 6 && items[0].sectors == [6, 7, 8, 9], "first sector / list")
check(items[1].detail == "시작 방향에서 왼쪽 45° 부근", "left \(items[1].detail)")        // (20+1)*15=315 -> -45
check(SpaceReview.needsReview(gp), "needs review")
// wrap-around problem: 22,23,0,1
var gw = gp
for s in [22, 23, 0, 1] { gw.clear(ring: 1, sector: s) }
let wi = SpaceReview.items(gw, startHeadingRad: 0).first { $0.sectors.contains(0) }!
check(wi.title == "벽 구간 23~2 · 비어 있음" || wi.title.hasPrefix("벽 구간 21"), "wrap title \(wi.title)")
// relative to start heading
let it2 = SpaceReview.describe(QualityGrid.Problem(ring: 1, start: 6, length: 4, kind: .missing), startHeadingRad: 90 * .pi / 180)
check(it2.detail.contains("시작 방향에서 오른쪽 30°"), "relative to start heading: \(it2.detail)")
let it3 = SpaceReview.describe(QualityGrid.Problem(ring: 1, start: 0, length: 24, kind: .missing), startHeadingRad: 0)
check(it3.title == "벽 전체 · 비어 있음", "full ring")
check(SpaceReview.sideText(deg: 3) == "정면" && SpaceReview.sideText(deg: -178).hasPrefix("정반대") && SpaceReview.sideText(deg: -47) == "왼쪽 45°", "sideText")
// overall < 0.8 triggers review even without... (all ok but a missing ring)
var go = SpaceGridMap.newGrid()
for s in 0..<24 { for _ in 0..<4 { go.add(ring: 1, sector: s, quality: 0.9) } }
check(SpaceReview.needsReview(go), "walls only -> problems (floor/ceiling missing)")

// ── target satisfaction
var gtgt = gp
let tg = RescanTarget(ring: 1, sectors: items[0].sectors)
check(tg.next(in: gtgt) == 6 && !tg.satisfied(in: gtgt), "target first = 6")
for _ in 0..<4 { SpaceGridMap.add(to: &gtgt, forward: fwd(az: 97.5), quality: 0.9) }   // sector 6 centre -> fills 5,6,7
check(tg.next(in: gtgt) == 8, "pan fills 6,7 -> next 8 (\(String(describing: tg.next(in: gtgt))))")
for _ in 0..<4 { SpaceGridMap.add(to: &gtgt, forward: fwd(az: 142.5), quality: 0.9) }  // sector 9 -> 8,9,10
check(tg.satisfied(in: gtgt), "target satisfied")

// ── guide / hint
var gh = SpaceGridMap.newGrid()
check(SpaceGuide.hint(grid: gh, headingRad: 0, viewRing: 1, target: nil).contains("시작하세요"), "empty hint")
check(SpaceGuide.hint(grid: gh, headingRad: 0, viewRing: 1, target: nil, facility: true).contains("시설물"), "facility wording")
for s in 0..<24 where !(6...9).contains(s) { for _ in 0..<4 { gh.add(ring: 1, sector: s, quality: 0.9) } }
for r in [0, 2] { for s in 0..<24 { for _ in 0..<4 { gh.add(ring: r, sector: s, quality: 0.9) } } }
let h0 = SpaceGuide.hint(grid: gh, headingRad: 7.5 * .pi / 180, viewRing: 1, target: nil)
check(h0.contains("벽 7~10번 구간이 비어 있습니다") && h0.contains("오른쪽으로 약 90° 돌아"), "hint right: \(h0)")
let h1 = SpaceGuide.hint(grid: gh, headingRad: 250 * .pi / 180, viewRing: 1, target: nil)
check(h1.contains("왼쪽"), "hint left: \(h1)")
let h2 = SpaceGuide.hint(grid: gh, headingRad: 112.5 * .pi / 180, viewRing: 1, target: nil)
check(h2.contains("지금 방향"), "hint here: \(h2)")
let cell = SpaceGuide.pick(grid: gh, headingRad: 0, target: nil)
check(cell == GuideCell(ring: 1, sector: 6), "pick nearest = 6 \(String(describing: cell))")
let dd = GuideMath.deltaDeg(headingRad: 0, targetSector: 6, sectors: 24)
check(abs(dd - 97.5) < 0.01, "deltaDeg 97.5")
var gc = gh; for s in 0..<24 { gc.clear(ring: 2, sector: s) }
let cc = SpaceGuide.pick(grid: gc, headingRad: 0, target: RescanTarget(ring: 2, sectors: [5, 6]))
check(cc == GuideCell(ring: 2, sector: 5), "target overrides")
check(SpaceGuide.vertical(for: cc, viewRing: 1) == .up && SpaceGuide.vertical(for: cc, viewRing: 2) == .none, "vertical up only when not looking at ceiling")
check(SpaceGuide.hint(grid: gc, headingRad: 0, viewRing: 1, target: RescanTarget(ring: 2, sectors: [5])).contains("위쪽"), "ceiling hint says up")
let full = SpaceGridMap.newGrid(); var gf = full
for r in 0..<3 { for s in 0..<24 { for _ in 0..<4 { gf.add(ring: r, sector: s, quality: 0.9) } } }
check(SpaceGuide.hint(grid: gf, headingRad: 0, viewRing: 1, target: nil).contains("충분"), "done hint")
check(SpaceGuide.vertical(for: nil, viewRing: 1) == .none, "no cell no arrow")

// ── persistence (Codable round trip)
let enc = try! JSONEncoder().encode(gp), dec = try! JSONDecoder().decode(QualityGrid.self, from: enc)
check(dec == gp, "grid codable round trip")
check(SpaceGridMap.meanQuality(SpaceGridMap.newGrid()) == nil && abs((SpaceGridMap.meanQuality(gf) ?? 0) - 0.9) < 1e-4, "meanQuality")

print(fails == 0 ? "ALL OK" : "\(fails) FAILED")
