# shared

서비스 간 **API Contract**(JSON Schema, draft 2020-12)를 관리한다. 모든 서비스는 이 스키마를 단일 진실 원천(single source of truth)으로 삼는다.

| Schema | Producer → Consumer |
|---|---|
| [`schemas/frame-envelope.schema.json`](schemas/frame-envelope.schema.json) | C1 Gateway → C2 Preprocessing |
| [`schemas/processed-frame.schema.json`](schemas/processed-frame.schema.json) | C2 Preprocessing → Nginx → C3 Inference |
| [`schemas/detection-result.schema.json`](schemas/detection-result.schema.json) | C3 Inference → C4 Visualizer |

상세 설명은 [`docs/api-contract.md`](../docs/api-contract.md) 참고.

## 변경 규칙
- 스키마 변경은 PR로만 진행하고, 영향받는 서비스 owner 전원의 리뷰를 받는다.
- 하위 호환이 깨지는 변경은 `schema_version` major를 올린다.
- 공통 필드(`schema_version`, `trace_id`, `session_id`, `frame_id`, `captured_at_ms`)는 모든 단계에서 **변경 없이 전파**한다.

## TODO
- [ ] 각 서비스에서 스키마 기반 모델(pydantic 등) 생성/검증 방식 결정
- [ ] 샘플 payload(`examples/`) 추가
