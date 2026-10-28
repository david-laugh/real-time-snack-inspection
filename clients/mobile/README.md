# Mobile Client

스마트폰 카메라 영상을 C1 Gateway(`WS /ws/stream`)로 실시간(목표 30fps) 송출하는 클라이언트.

## 미정 사항 (TODO)
- [ ] 구현 방식 선정: 네이티브 앱 / 모바일 브라우저(WebRTC·getUserMedia + WebSocket) / 기존 IP Webcam 앱 활용
- [ ] 프레임 인코딩(JPEG 품질, 해상도) 결정
- [ ] `captured_at_ms` 기록 및 서버와의 시계 동기화 방법
- [ ] 같은 네트워크(LAN)에서 C1 접속 방법 문서화 (`ws://<host-ip>:8000/ws/stream`)

현재 디렉터리는 문서만 포함한다. 개발 전까지는 [`../simulator`](../simulator/README.md)로 대체한다.
