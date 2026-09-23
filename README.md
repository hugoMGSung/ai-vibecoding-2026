# ai-vibecoding-2026

바이브코딩 리포지토리

## Chapter 1

AI에게 코딩을 시키자, 제대로!

### 개념

코딩을 직접하지는 말 것. AI와 협업해서 새로운 프로그램을 만들자

#### 기존 개발 방법

요구사항 분석 -> 설계(DB/UI 포함) -> 구현/디버깅 -> 테스트 -> 배포 -> 유지보수

#### 바이브코딩 방식

요구사항정의(PRD) -> AI 코드 생성/디버깅,테스트 -> 사람 **검증** 수정 요청, 직접 수정 -> 배포 -> AI 유지보수

#### 핵심 포인트

- AI - 주니어/시니어 개발
- 사람 - PM + 리뷰어

### 바이브코딩 개발환경

- VS Code, VS Code Insider, Android Studio, ...

#### VS Code

- 채팅 창 - 안씀
- 확장 패키지 - Codex, Claude Code for VS Code, Gemini Code Assist

#### Codex

- 설치 후 확장 아이콘 아래, Codex 아이콘 생성 됨

![](assets/20260917_170818_image.png)

- 로그인 - 웹 브라우저 연결
- 설정화면 설정 필요

![](assets/20260917_171229_image.png)

- 추가파일 Codex-*-sandbox.exe 파일 설치

![](assets/20260917_171318_image.png)

- 최종 화면
- 채팅 창 명령 / 여러 LLM에 전달할 명령어 리스트

#### 바이브코딩 맛보기

![](assets/20260917_172319_image.png)

- 제로샷 프롬프트로 요청

![](assets/20260917_172409_image.png)

- 결과 메시지 화면
- 소스

#### CLI Codex

- 파워쉘, 콘솔 창에서 명령어로 수행하는 Codex

#### 바이브 코딩

- 제로샷 프롬프트 : 아무런 기초지식없이 대화로 바이브코딩
- 원샷 프롬프트 : 적어도 한줄의 요구사항을 작성해서 바이브코딩
- 퓨샷 프롬프트 : PRD를 작성해서 바이브코딩


#### 프롬프트 사용법

- 이미지를 캡쳐해서 복사/붙여넣기 후 프롬프트 사용
- 특정 소스코드를 선택한 뒤 우클릭으로` Add to Codex Threaed` 선택 훠 프롬프트 사용

### 주식 자동매매 개발환경

#### 토스증권 OpenAPI

- https://corp.tossinvest.com/ko/open-api
- 토스앱 모바일 설치 가입
- 토스증권 사용 설정
- 토스증권 PC 웹사이트 동작
- 사용중인 아이피를 토스증권 PC 등록
- OpenAPI 키 발급 후 ClientID, Client Secret 문자열 보관
- https://developers.tossinvest.com/docs

#### API 신청

- https://corp.tossinvest.com/ko/open-api
- PC에서 투자하기 클릭
- 토스앱 모바일로 로그인 인증
- 오른쪽 하단 기어모양 아이콘(설정)

![](assets/20260921_170830_image.png)

- Client Id, `Client Secret`, IP 추가
- cmd > ipconfig로 보인 아이피 확인 후 추가

### 주식 자동매매 파이썬 프로그램 분석

- `__init__.py` - 일반적으로 파일만 생성. 소스코드 X 프로젝트 폴더가 pip로 설치할 수 있는 패키지화
- `__main__.py` - 파이썬으로 실행될때 가장 먼저 실행되는 메인 함수 파일
- `__pycache__` - 미리 만들어놓은 파이썬 실행 파일(캐시)
- tests - 소스코드 테스트 실행을 위한 폴더
- .env.example - 환경설정 예제파일 .example을 지우고 사용
  `.env` 는 깃허브에 업로드 방지위해 .gitignore에 제외파일로 등록
- requirements.txt - 파이썬 개발환경 패키지 설치리스트 파일
  - `pip install -r requirements.txt` 로 전부 설치

#### HTTP 403 문제

![](assets/20260923_112110_image.png)

- 공인아이피 확인 - findip.kr 등 사이트에서 확인

![](assets/20260923_114916_image.png)

- 토스 API 설정화면에서 IP 등록

![](assets/20260923_115030_image.png)

- 실행화면

![](assets/20260923_114044_image.png)

#### favicon.ico 작업

- https://flaticon.com 에서 원하는 이미지 png 다운로드
- https://convertio.co 에서 png를 ico로 변환. 다운로드
- favicon.ico로 이름 변경
- static 폴더에 복사
- index.html에 아래코드 추가

```html
<title>Toss Auto Trader</title>
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
```
