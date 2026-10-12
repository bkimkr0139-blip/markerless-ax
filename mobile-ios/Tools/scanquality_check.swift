// 실행: mobile-ios 폴더에서  mkdir -p /tmp/sqc && cp Tools/scanquality_check.swift /tmp/sqc/main.swift && swiftc -o /tmp/sqc/run Markerless/Capture/ScanQualityKit.swift /tmp/sqc/main.swift && /tmp/sqc/run
import Foundation

var fails = 0
func check(_ c: Bool, _ m: String) { if !c { fails += 1; print("FAIL: \(m)") } }

// --- 격자 상태 전이
var g = QualityGrid(rings: 3, sectors: 24, need: 2)
check(g.state(1, 0) == .missing, "초기=누락")
var t = g.add(ring: 1, sector: 0, quality: 0.9)
check(t.old == .missing && t.new == .low, "1프레임=낮음(부족)")
t = g.add(ring: 1, sector: 0, quality: 0.9)
check(t.new == .ok, "2프레임·고품질=양호 (\(t.new))")
t = g.add(ring: 1, sector: 0, quality: 0.9)
check(t.new == .great, "3프레임·0.9=좋음")
for _ in 0..<5 { g.add(ring: 1, sector: 1, quality: 0.2) }
check(g.state(1, 1) == .low, "프레임 많아도 품질 낮으면 낮음")
check(g.fill(1, 1) <= 0.6, "낮은 품질은 채움 표시 상한 0.6")
g.add(ring: 1, sector: 100, quality: 0.8)  // 섹터는 모듈로
check(g.cell(1, 100 % 24).frames == 1, "섹터 모듈로")
check(g.add(ring: 9, sector: 0, quality: 1).new == .missing, "범위 밖 링 무시")
check(g.add(ring: 0, sector: 0, quality: .nan).new == .missing, "NaN 무시")

// --- 문제 목록: 원형으로 이어지는 구간 합치기
var w = QualityGrid(rings: 1, sectors: 12, need: 1)
for s in 0..<12 where ![0, 1, 11, 6].contains(s) { w.add(ring: 0, sector: s, quality: 0.9) }  // 비어 있음: 11,0,1 (이어짐) + 6
let ps = w.problems().filter { $0.kind == .missing }
check(ps.count == 2, "누락 구간 2개 (\(ps.count))")
check(ps.contains { $0.length == 3 && $0.start == 11 }, "11→0→1 순환 구간 합침")
check(ps.contains { $0.length == 1 && $0.start == 6 }, "단독 6")
check(ps.first { $0.length == 3 }!.sectorList(12) == [11, 0, 1], "구간 섹터 목록")
var allBad = QualityGrid(rings: 1, sectors: 6, need: 1)
check(allBad.problems() == [QualityGrid.Problem(ring: 0, start: 0, length: 6, kind: .missing)], "전부 비면 한 구간")
allBad.reset()

// --- 가장 가까운 문제 + 방향 부호
var n = QualityGrid(rings: 1, sectors: 24, need: 1)
for s in 0..<24 where s != 5 && s != 20 { n.add(ring: 0, sector: s, quality: 0.9) }
let near = n.nearestProblem(from: 3)!
check(near.sector == 5 && near.delta == 2, "3에서 5는 +2 (\(near))")
let near2 = n.nearestProblem(from: 22)!
check(near2.sector == 20 && near2.delta == -2, "22에서 20은 −2 (\(near2))")
let wrap = n.nearestProblem(from: 23)!
check(wrap.sector == 20 && wrap.delta == -3, "원형 최단 (\(wrap))")
check(GuideMath.degrees(delta: -3, sectors: 24) == -45, "도 환산")
check(n.nearestProblem(from: 0, kinds: [.missing], rings: [0])?.ring == 0, "링 제한")

// --- 전체 진행도 가중
var o = QualityGrid(rings: 3, sectors: 4, need: 1)
for s in 0..<4 { o.add(ring: 1, sector: s, quality: 0.9) }  // 링1 100%
check(abs(o.overall(weights: [1, 2, 1]) - 0.5) < 1e-5, "가중 평균 0.5 (\(o.overall(weights: [1, 2, 1])))")

// --- 실시간 품질
var ok = LiveQuality.Input(); ok.distanceM = 1.5; ok.depthConf = 1.9; ok.depthValid = 0.9; ok.ambient = 200
let good = LiveQuality.evaluate(ok)
check(good.level == .good && good.reasons.isEmpty && good.score > 0.9, "정상 입력=좋음 (\(good))")
var tr = ok; tr.trackingNormal = false
check(LiveQuality.evaluate(tr).primary == .tracking && LiveQuality.evaluate(tr).level == .bad, "추적 불량=최우선·나쁨")
var fast = ok; fast.speedMPerS = 0.9
let f = LiveQuality.evaluate(fast)
check(f.primary == .tooFast && f.level != .good, "빠름 (\(f))")
var close = ok; close.distanceM = 0.2
check(LiveQuality.evaluate(close).primary == .tooClose, "너무 가까움")
var far = ok; far.distanceM = 4.8
check(LiveQuality.evaluate(far).primary == .tooFar, "너무 멂 (\(LiveQuality.evaluate(far)))")
var farObj = ok; farObj.distanceM = 2.0
check(LiveQuality.evaluate(farObj, profile: .object).primary == .tooFar && LiveQuality.evaluate(farObj, profile: .space).level == .good, "프로필별 거리 기준")
var dark = ok; dark.ambient = 10
check(LiveQuality.evaluate(dark).primary == .dark, "어두움")
var conf = ok; conf.depthConf = 0.4
check(LiveQuality.evaluate(conf).primary == .lowConfidence, "깊이 신뢰도")
var multi = ok; multi.speedMPerS = 0.8; multi.blur = 0.5
check(LiveQuality.evaluate(multi).reasons.count >= 2 && LiveQuality.evaluate(multi).score < f.score, "복합 요인은 점수가 더 낮고 이유가 여러 개")
check(LiveQuality.evaluate(fast).score >= 0 && LiveQuality.evaluate(fast).score <= 1, "점수 범위")

// --- 표시 완화: 나빠짐은 빨리, 좋아짐은 천천히, 한 번의 튐은 무시
var lf = LevelFilter()
let bad = LiveQuality.Output(score: 0.1, level: .bad, reasons: [.tooFast])
let gd = LiveQuality.Output(score: 1, level: .good, reasons: [])
var changes: [LevelFilter.Change] = []
for _ in 0..<10 { if let c = lf.push(gd) { changes.append(c) } }
check(changes.isEmpty && lf.level == .good, "안정 구간은 변화 없음")
if let c = lf.push(bad) { changes.append(c) }
check(changes.isEmpty, "한 번 튄 값은 무시(EMA)")
for _ in 0..<6 { if let c = lf.push(bad) { changes.append(c) } }
check(changes.contains { $0.worse }, "지속되면 나빠짐 감지")
check(lf.level != .good, "레벨 하락 반영")
var recovered = false
for _ in 0..<12 { if let c = lf.push(gd), !c.worse { recovered = true } }
check(recovered && lf.level == .good, "회복도 결국 반영")

// --- 화살표 계산
check(abs(GuideMath.deltaDeg(headingRad: 0, targetSector: 6, sectors: 24) - 97.5) < 0.01, "시선 0°, 목표 6번 중심 97.5°")
check(GuideMath.deltaDeg(headingRad: 6.0, targetSector: 0, sectors: 24) > 0, "시선 344°→목표 7.5° = + 방향(원형 최단)")
check(GuideMath.inView(deltaDeg: 10) && !GuideMath.inView(deltaDeg: 90), "화면 안 판정")

print(fails == 0 ? "ALL OK" : "\(fails) FAIL")
exit(fails == 0 ? 0 : 1)
