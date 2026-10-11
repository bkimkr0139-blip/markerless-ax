// CardThumbView.swift — 자산의 흑백 사진 실루엣 썸네일 (후보 목록·자산 관리에서 "어떤 사물인지" 한눈에)

import SwiftUI

struct CardThumbView: View {
    @EnvironmentObject var app: AppState
    let assetId: String
    var size: CGFloat = 48

    @State private var image: UIImage?
    @State private var loaded = false

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 8).fill(Color(white: 0.18))
            if let image {
                Image(uiImage: image).resizable().scaledToFit().padding(3)
            } else {
                Image(systemName: loaded ? "cube.transparent" : "ellipsis")
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: size, height: size)
        .task(id: assetId) {
            // 앞면 카드를 우선, 없으면 있는 첫 장
            guard let list = try? await app.api.assetPhotos(assetId: assetId) else { loaded = true; return }
            let pick = list.photos.first { $0.side == "a00" || $0.side == "front" } ?? list.photos.first
            if let p = pick, let data = try? await app.api.assetPhotoPNG(assetId: assetId, side: p.side) {
                image = UIImage(data: data)
            }
            loaded = true
        }
    }
}
