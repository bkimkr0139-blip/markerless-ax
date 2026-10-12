// 실행: Tools 폴더에서  mkdir -p /tmp/oqc && cp objscan_quality_check.swift /tmp/oqc/main.swift && swiftc -o /tmp/oqc/run ../Markerless/Capture/ScanQualityKit.swift ../Markerless/Capture/ScanCoverage.swift /tmp/oqc/main.swift && /tmp/oqc/run
// 사물 스캔 품질 인식 커버리지(ScanCoverage + QualityGrid) 로직 시험: 표본 품질·이웃 채움·좌우 안내 부호·문제→한국어 면 이름·재스캔 만족·기존 complete/canReview
import Foundation
import simd

var fails = 0
func check(_ c: Bool, _ m: String) { if !c { fails += 1; print("FAIL", m) } }

let center = SIMD3<Float>(0, 0, 0)
/// 대상 중심 기준: 방위(°, 0 = 시작 위치, 사람의 왼쪽으로 증가), 고도(°), 거리(m)의 카메라 위치 + 중심을 향한 시선
func cam(_ azDeg: Float, el: Float = 35, d: Float = 0.8) -> (SIMD3<Float>, SIMD3<Float>) {
    let a = azDeg * .pi / 180, e = el * .pi / 180
    let p = SIMD3<Float>(sin(a) * cos(e) * d, sin(e) * d, -cos(a) * cos(e) * d)
    return (p, simd_normalize(center - p))
}
func newCov() -> ScanCoverage {
    var c = ScanCoverage()
    let (p, _) = cam(0)
    c.setOrigin(camPos: p, center: center)
    return c
}
let clean = LiveQuality.Input()
var poor = LiveQuality.Input(); poor.speedMPerS = 0.6; poor.blur = 0.5   // 빠르고 흔들림
func sample(_ c: inout ScanCoverage, az: Float, el: Float = 35, d: Float = 0.8, n: Int, base: LiveQuality.Input = LiveQuality.Input()) {
    let (p, f) = cam(az, el: el, d: d)
    for _ in 0..<n { c.add(camPos: p, camFwd: f, center: center, base: base) }
}
func fillSector(_ c: inout ScanCoverage, _ s: Int, ring: Int = 1, q: Float = 0.9, n: Int = 4) { for _ in 0..<n { c.grid.add(ring: ring, sector: s, quality: q) } }

// 1) 품질 인식: 같은 위치·횟수라도 좋은 표본 vs 나쁜 표본은 다른 상태
do {
    var good = newCov(), bad = newCov()
    sample(&good, az: 37.5, n: 4)            // 섹터 2 중앙 (경계에서 멀다)
    sample(&bad, az: 37.5, n: 4, base: poor)
    check(good.grid.state(1, 2) >= .ok, "good samples -> ok (\(good.grid.state(1, 2)))")
    check(bad.grid.state(1, 2) == .low, "poor samples -> low (\(bad.grid.state(1, 2)))")
    check(good.isFilled(band: 1, sector: 2) && !bad.isFilled(band: 1, sector: 2), "isFilled follows quality")
    // 표본 수 부족 = low
    var few = newCov(); sample(&few, az: 37.5, n: 2)
    check(few.grid.state(1, 2) == .low, "2 frames -> low")
    // 멀리서(2.2m)는 거리 감점으로 품질 낮음, 범위 밖(3m)은 아예 불인정
    var far = newCov(); sample(&far, az: 37.5, d: 2.2, n: 6)
    check(far.grid.state(1, 2) <= .low, "far 2.2m not ok (\(far.grid.state(1, 2)))")
    var way = newCov(); sample(&way, az: 37.5, d: 3.0, n: 6)
    check(way.grid.cell(1, 2).frames == 0, "3m rejected")
    // 조준: 시선이 중심에서 벗어나면(lookDot<0.7) 불인정, 0.8쯤이면 품질 감쇠
    var off = newCov(); let (p, _) = cam(37.5); let fOff = simd_normalize(simd_normalize(center - p) + SIMD3<Float>(0.9, 0, 0))
    for _ in 0..<6 { off.add(camPos: p, camFwd: fOff, center: center) }
    check(off.grid.cell(1, 2).frames == 0, "aim away rejected")
    // 고도 띠: 20°=0, 40°=1, 70°=2
    var bands = newCov(); sample(&bands, az: 37.5, el: 20, n: 1); sample(&bands, az: 37.5, el: 40, n: 1); sample(&bands, az: 37.5, el: 70, n: 1)
    check(bands.grid.cell(0, 2).frames == 1 && bands.grid.cell(1, 2).frames == 1 && bands.grid.cell(2, 2).frames == 1, "elevation bands 0/1/2")
}

// 2) 이웃 채움: 경계 근처 표본은 이웃 칸에도 낮은 가중으로 들어가고, 칸 중앙 표본은 안 들어간다
do {
    var c = newCov()
    sample(&c, az: 37.5, n: 4)   // 중앙
    check(c.grid.cell(1, 1).frames == 0 && c.grid.cell(1, 3).frames == 0, "center sample: no neighbour")
    var e = newCov()
    sample(&e, az: 30.5, n: 4)   // 섹터 2의 시작 경계 (frac≈0.03) → 섹터 1에도
    check(e.grid.cell(1, 2).frames == 4 && e.grid.cell(1, 1).frames == 4, "edge sample feeds neighbour 1")
    check(e.grid.cell(1, 1).avg < e.grid.cell(1, 2).avg, "neighbour weight lower")
    var w = newCov()
    sample(&w, az: 0.5, n: 4)    // 섹터 0 시작 → 섹터 23 (원형 이음)
    check(w.grid.cell(1, 23).frames == 4, "wraps to sector 23")
    // 부드러운 한 바퀴(2°씩) → 모든 방위가 채워진다, 끊김 없음
    var orbit = newCov()
    var a: Float = 0
    while a < 360 { sample(&orbit, az: a + 0.1, n: 2); a += 2 }
    check(orbit.azimuthFilled == 24, "orbit fills all (\(orbit.azimuthFilled))")
    check(orbit.complete && orbit.canReview, "orbit complete")
    check(!orbit.needsReview(), "full orbit: no review needed")
}

// 3) 기존 complete / canReview
do {
    var c = newCov()
    var a: Float = 0
    while a < 180 { sample(&c, az: a + 0.1, n: 2); a += 2 }   // 0..180° = 섹터 0...11
    check(c.canReview && !c.complete, "half orbit: canReview, not complete (\(c.azimuthFilled))")
    var few = newCov(); sample(&few, az: 7.5, n: 4)
    check(!few.canReview, "one spot: not canReview")
    check(c.needsReview(), "half orbit -> needs review")
    // legacy: 깨끗한 표본 3개면 칸이 채워지고 add()가 true
    var l = newCov(); let (p, f) = cam(37.5)
    check(!l.add(camPos: p, camFwd: f, center: center) && !l.add(camPos: p, camFwd: f, center: center), "first two: not yet")
    check(l.add(camPos: p, camFwd: f, center: center), "third: cell ok -> true")
    // assist 는 그대로 동작 (둘레를 돌았으면 완료 문구)
    var full = newCov(); a = 0; while a < 360 { sample(&full, az: a + 0.1, n: 2); a += 2 }; sample(&full, az: 10, el: 70, n: 3)
    let (p2, f2) = cam(10)
    check(full.assist(camPos: p2, camFwd: f2, center: center, tracking: true, tooFast: false).level != .warn, "assist ok on full")
}

// 4) 좌우 안내 부호: 방위 번호가 늘어나는 쪽 = 사람의 왼쪽 → edgeDeg 음수(왼쪽), 줄어드는 쪽 = 오른쪽 양수
do {
    var c = newCov()
    for s in 0..<24 where s != 6 { fillSector(&c, s) }
    let gl = c.guidance(camSector: 3, rescan: nil)
    check(gl?.sector == 6 && gl?.deltaSectors == 3, "missing 6 from 3: delta +3 (\(String(describing: gl)))")
    check((gl?.edgeDeg ?? 0) == -45, "left of person = negative edge deg (\(String(describing: gl?.edgeDeg)))")
    var d = newCov()
    for s in 0..<24 where s != 20 { fillSector(&d, s) }
    let gr = d.guidance(camSector: 3, rescan: nil)
    check(gr?.sector == 20 && gr?.deltaSectors == -7, "missing 20 from 3: delta -7")
    check((gr?.edgeDeg ?? 0) == 105, "right = positive edge deg (\(String(describing: gr?.edgeDeg)))")
    // 실제 카메라 방위와도 일치: ScanCoverage.assist 가 같은 방향을 말한다
    let (pc, fc) = cam(3 * 15 + 7.5)
    var h = newCov(); for s in 0..<24 where !(10...14).contains(s) { fillSector(&h, s) }  // 한 바퀴 미완(19칸)
    let ast = h.assist(camPos: pc, camFwd: fc, center: center, tracking: true, tooFast: false)
    check(ast.text.contains("왼쪽") && ast.targetSector == 10, "assist agrees: left (\(ast.text))")
    check(h.guidance(camSector: 3, rescan: nil)?.sector == 10 && (h.guidance(camSector: 3, rescan: nil)?.edgeDeg ?? 0) < 0, "guidance agrees with assist")
    // 윗면만 비었으면 위 화살표
    var t = newCov(); for s in 0..<24 { fillSector(&t, s) }
    let gu = t.guidance(camSector: 5, rescan: nil)
    check(gu?.up == true && gu?.edgeDeg == nil, "only top missing -> up arrow")
    // 처음(둘레 40% 미만)엔 안내 없음
    var early = newCov(); for s in 0..<4 { fillSector(&early, s) }
    check(early.guidance(camSector: 1, rescan: nil) == nil, "no guide at start")
}

// 5) 문제 → 한국어 면 이름 (0°=앞, 90°=왼쪽, 180°=뒤, 270°=오른쪽)
do {
    check(ScanCoverage.sideName(start: 23, length: 2) == "앞면", "front")
    check(ScanCoverage.sideName(start: 5, length: 2) == "왼쪽 면", "left 90")
    check(ScanCoverage.sideName(start: 11, length: 2) == "뒷면", "back 180")
    check(ScanCoverage.sideName(start: 17, length: 2) == "오른쪽 면", "right 270")
    check(ScanCoverage.sideName(start: 8, length: 2) == "왼쪽 뒷면", "left-back")
    var c = newCov()
    for s in 0..<24 { fillSector(&c, s) }
    for s in 10...14 { c.grid.cells[c.grid.idx(1, s)] = QualityCell() }                       // 뒤: 비어 있음
    for s in 4...5 { c.grid.cells[c.grid.idx(1, s)] = QualityCell(frames: 5, sumQ: 5 * 0.3) }  // 왼쪽: 품질 낮음
    let ds = c.problemDescs()
    check(ds.contains { $0.title == "뒷면 · 비어 있음" && $0.kind == .missing && $0.length == 5 }, "back missing title \(ds.map(\.title))")
    check(ds.contains { $0.title == "왼쪽 면 · 품질 낮음" && $0.kind == .low }, "left low title")
    check(ds.contains { $0.title == "윗면 · 비어 있음" && $0.ring == 1 }, "top item")
    check(c.needsReview(), "problems -> review")
    // 옆(링0)에서 찍은 칸과 비스듬(링1)에서 찍은 칸은 합산해 한 방위로 본다
    var m = newCov()
    for s in 0..<24 { fillSector(&m, s, ring: s % 2 == 0 ? 0 : 1) }
    check(m.azimuthProgress == 1 && m.problemDescs().allSatisfy { $0.ring == 1 }, "merged bands cover azimuth")
}

// 6) 재스캔 목표 만족 + 낮은 칸 초기화
do {
    var c = newCov()
    for s in 0..<24 { fillSector(&c, s) }
    for s in 10...14 { c.grid.cells[c.grid.idx(1, s)] = QualityCell() }
    for s in 4...5 { c.grid.cells[c.grid.idx(1, s)] = QualityCell(frames: 5, sumQ: 5 * 0.3) }
    let back = c.problemDescs().first { $0.title.hasPrefix("뒷면") }!
    let tb = c.rescanTarget(for: back)
    check(tb.sectors == [10, 11, 12, 13, 14] && !c.isSatisfied(tb), "back target pending")
    let g1 = c.guidance(camSector: 3, rescan: tb)
    check(g1?.sector == 10 && g1?.deltaSectors == 7, "rescan guide nearest remaining (\(String(describing: g1)))")
    for az in [157.5, 172.5, 187.5, 202.5, 217.5] { sample(&c, az: Float(az), n: 4) }
    check(c.isSatisfied(tb), "back satisfied after good scans (\(c.remaining(tb)))")
    check(c.guidance(camSector: 3, rescan: tb) == nil, "no guide when satisfied")
    // 품질 낮음 → 재스캔 시작 시 비우고, 좋은 표본 몇 번이면 만족 (옛 평균에 안 끌려감)
    let left = c.problemDescs().first { $0.title.hasPrefix("왼쪽") }!
    let tl = c.rescanTarget(for: left)
    c.prepareRescan(tl)
    check(c.reviewGrid.state(0, 4) == .missing, "low cleared at rescan start")
    sample(&c, az: 67.5, n: 4); sample(&c, az: 82.5, n: 4)
    check(c.isSatisfied(tl), "left satisfied after fresh good scans")
    // 윗면 목표: 30% 이상 확보되면 만족
    var t = newCov(); for s in 0..<24 { fillSector(&t, s) }
    let top = t.problemDescs().first { $0.ring == 1 }!
    let tt = t.rescanTarget(for: top)
    check(!t.isSatisfied(tt), "top pending")
    for s in 0..<8 { fillSector(&t, s, ring: 2) }
    check(t.isSatisfied(tt), "top satisfied at 33%")
    check(t.problemDescs().isEmpty, "no problems left")
}

print(fails == 0 ? "ALL OK" : "\(fails) FAILED")
exit(fails == 0 ? 0 : 1)
