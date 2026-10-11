# revit-addon (스텁)

실 BIM(RVT)을 구할 수 없는 본 PoC에서는 **스캔 기반 자가 에셋**이 BIM/CAD를 대체한다
(지시서 2.2 대안 절차). 본 폴더는 실 Revit 연동 시의 진입점 스텁만 둔다.

## 목적 (실 연동 시)

Revit 플러그인(C#)이 선택 요소의 설치 좌표/공차를 **mobile-package JSON**으로 내보낸다.
스키마: `schemas/mobile-package.schema.json` — 백엔드 `POST /api/v1/packages`의 입력과 동일.

## PoC에서의 위치

- `test-data/mobile-package-r12.json` 샘플이 이 스키마의 예시다 (서버 E2E 테스트가 검증)
- 백엔드 packages 라우터는 tolerance/orientation 누락 시 422(BIM 데이터 오류)로 거부 — 지시서 12.2
- 실 구현 착수 시: Revit API `ExporterOptions`/`FilteredElementCollector` → 스키마 매핑 → GLB 변환은
  `Revit.GLTF` 오픈소스 또는 Navisworks 내보내기 경로

## C# 스텁

`ExportCommand.cs` — 명령 골격과 스키마 매핑 주석만 (빌드 대상 아님, PoC 스코프 외)
