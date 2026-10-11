# mobile-android (PoC 후반부 — 구조 스텁)

개발지시서 3단계: Galaxy (ARCore + Depth API) 클라이언트. PoC 범위에서는 **iOS 먼저**(지시서 2.1),
Android는 아키텍처만 확정해 둔다.

## 아키텍처 (확정 — DESIGN.md D1과 연동)

핵심 정합 엔진(ICP/판정/신뢰도)은 **서버(spatial-core) 공유** — Android 앱은 iOS와 동일한
REST API(docs/API.md)만 호출한다. 화면 구성도 동일하게 포팅한다:

| iOS 구성 | Android 대응 |
|----------|--------------|
| ARKit WorldTracking + SceneReconstruction(.mesh) | ARCore Geospatial 아님 — **ARCore Depth API + Plane/Point Cloud**, 메시는 `Depth` 기반 포인트클라우드 누적 (ARCore Sceneform 폐지 이후 Jetpack XR 또는 raw ARCore) |
| ARWorldMap | **없음** → 대안: 클라우드 앵커(Cloud Anchors) 또는 세션 시작 원점 고정(같은 장소 재시작) — T_space_session 해결이 Android의 최대 난점 (COORDINATE.md 4절의 Android 대응책 필요) |
| GLBWriter/Reader (Swift 자체 구현) | 동일 최소 GLB 포맷 — Kotlin으로 동일 구현 또는 sgct/ufgltf 경량 파서 |
| RealityKit 오버레이 | SceneView (Filament) + 오버레이 ModelEntity 위치 = designPose |
| APIClient (URLSession) | Retrofit/OkHttp — 동일 camelCase 스키마 |

## 데이터 호환 계약 (변경 금지)

- 좌표: ARCore도 Y-up 오른손 → iOS와 동일한 `frame:"space"` 클라우드 전송
- WorldMap 대응: 월드맵 PUT/GET는 iOS 전용 바이너리(bplist)이므로 Android는 `hasWorldMap=false` 공간에서
  **세션 시작을 원점으로** 측정하거나, 향후 공간별 원점 마커(QR) 배치로 T_space_session을 명시 복원

## 구현 순서 (Week 4~)

1. Kotlin 템플릿 + ARCore 세션/깊이 캡처
2. Depth 기반 클라우드 추출 → `POST /api/v1/measurements` (기존 백엔드 그대로)
3. 스캔 자산 등록 (`POST /api/v1/assets/scan`) — iOS와 동일 플로우
