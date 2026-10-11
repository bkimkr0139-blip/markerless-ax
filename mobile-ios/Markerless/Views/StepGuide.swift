// StepGuide.swift — 화면 상단 단계 안내 카드 (완료 ✓ / 현재 ● / 대기 ○ + "지금 할 일")
//
// 탭 이름(① 공간 → ② 스캔·등록 → ③ 검수)이 전체 작업 순서이고,
// 각 화면의 StepGuide는 그 탭 안에서의 세부 절차를 보여준다.

import SwiftUI

struct GuideStep: Identifiable {
    enum State { case done, current, todo }
    let id: Int
    let title: String
    let state: State
}

extension Array where Element == GuideStep {
    /// 완료 여부 배열로 단계 목록 생성 — 첫 미완료 단계가 "현재"
    static func make(_ titles: [String], done: [Bool]) -> [GuideStep] {
        let firstTodo = done.firstIndex(of: false)
        return titles.enumerated().map { i, t in
            let state: GuideStep.State = done[i] ? .done : (i == firstTodo ? .current : .todo)
            return GuideStep(id: i, title: t, state: state)
        }
    }
}

struct StepGuide: View {
    let heading: String
    let steps: [GuideStep]
    /// 지금 해야 할 일 (한 줄)
    let nextAction: String
    var warning: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(heading).font(.caption.bold()).foregroundStyle(.secondary)

            HStack(spacing: 4) {
                ForEach(steps) { s in
                    HStack(spacing: 4) {
                        Image(systemName: icon(s.state))
                            .foregroundStyle(color(s.state))
                        Text(s.title)
                            .font(.caption2.weight(s.state == .current ? .bold : .regular))
                            .foregroundStyle(s.state == .todo ? .secondary : .primary)
                    }
                    if s.id != steps.last?.id {
                        Image(systemName: "chevron.right").font(.system(size: 8)).foregroundStyle(.tertiary)
                    }
                }
            }

            Label(nextAction, systemImage: "arrow.right.circle.fill")
                .font(.footnote.bold())
                .foregroundStyle(.tint)

            if let warning {
                Text("⚠️ \(warning)").font(.caption).foregroundStyle(.red)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .padding(.horizontal, 10)
    }

    private func icon(_ s: GuideStep.State) -> String {
        switch s {
        case .done: return "checkmark.circle.fill"
        case .current: return "circle.inset.filled"
        case .todo: return "circle"
        }
    }

    private func color(_ s: GuideStep.State) -> Color {
        switch s {
        case .done: return .green
        case .current: return .blue
        case .todo: return .secondary
        }
    }
}
