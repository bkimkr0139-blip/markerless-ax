// ExportCommand.cs — Revit 플러그인 골격 (PoC 스코프 외, 빌드 대상 아님)
//
// 역할: 선택 요소(설비/기둥 등)의 설치 좌표·공차를 mobile-package JSON으로 내보내
// 백엔드 POST /api/v1/packages로 임포트한다 (스키마: schemas/mobile-package.schema.json).
//
// 좌표 주의 (docs/COORDINATE.md):
//   Revit = Z-up 피트/인치 내부 → 내보낼 때 미터 변환 + 필요 시 Z_UP_TO_Y_UP.
//   PoC 백엔드는 공간 프레임 좌표를 그대로 받으므로 T_bim_space를 별도 필드로 확장하는
//   것이 원칙(DESIGN.md D-좌표 체인). 본 스텁은 공간=Revit 공유좌표 단순 가정.
//
// 공차 주의 (지시서 12.2): 요소별 tolerance 누락 시 예외 → 422가 정상 동작이다.
//   기본값 부여 위치는 "BIM 관리자 검토 후" 여야 하며 플러그인이 조용히 채우지 않는다.

using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace MarkerlessPoc.Addon
{
    [Transaction(TransactionMode.Manual)]
    public class ExportCommand : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            var uidoc = commandData.Application.ActiveUIDocument;
            var doc = uidoc.Document;

            var selected = uidoc.Selection.GetElementIds()
                .Select(id => doc.GetElement(id))
                .Where(e => e is FamilyInstance)
                .ToList();

            if (selected.Count == 0)
            {
                message = "내보낼 FamilyInstance를 선택하세요.";
                return Result.Failed;
            }

            // TODO(Week 5+): 요소별로
            //   1. LocationPoint.Point (피트) → 미터 변환 (×0.3048)
            //   2. 방위 → rpyDeg (intrinsic Z-Y-X, 단위 도)
            //   3. 공차: 공유 파라미터 "MARKERLESS_TOL_MM"/"MARKERLESS_TOL_ROT_DEG"에서 읽기
            //      없으면 스킵하고 리포트(조용히 기본값 채우지 않음 — 지시서 12.2)
            //   4. GLB 변환 (Revit.GLTF 또는 미리 추출된 CAD 대체)
            //   5. schemas/mobile-package.schema.json 형태로 직렬화 → POST /api/v1/packages
            throw new NotImplementedException("PoC 범위 외 — README.md 참조");
        }
    }
}
