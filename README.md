# eval-assistant — 경량 LLM 평가용 한국어 질문지 생성 + E2E 검수 도우미

날씨·로컬·금융 도메인의 한국 특화 평가 질문을 자동 생성하고,
카나나 샌드박스 앱 검수(복사 → 붙여넣기 → 응답 기록)를 한 화면에서 처리한 뒤
카카오 제출용 xlsx까지 추출하는 로컬 도구입니다.

**핵심 설계**: LLM은 한국 지식을 몰라도 된다. 지명·종목·금융용어는 시드 데이터(`seeds.yaml`)가
주입하고, LLM(Qwen3 등)은 자연스러운 한국어 문장화만 담당한다. LLM이 없어도 템플릿 모드로 동작한다.
검수를 통과한 질문은 자동으로 few-shot 예시로 재사용되어 **쓸수록 생성 품질이 올라간다.**

## 설치 — WSL/Ubuntu 기준

### 1. Python 환경

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip

cd ~/Development/eval-assistant
python3 -m venv .venv                 # 가상환경 생성 (최초 1회)
source .venv/bin/activate             # 활성화 — 새 터미널을 열 때마다 실행
pip install -r requirements.txt
```

> **왜 venv?** 우분투 최신 버전은 시스템 파이썬에 pip 설치를 막습니다
> (`externally-managed-environment` 에러). venv를 쓰면 이 문제가 없고,
> `--break-system-packages`나 pip 전역 설정 변경 없이 깔끔하게 관리됩니다.

### 2. Ollama (LLM 모드용 — 선택)

```bash
sudo apt install -y zstd                          # 설치 스크립트가 요구함
curl -fsSL https://ollama.com/install.sh | sh     # 공식 설치 (snap 사용 금지: 구버전)
systemctl status ollama                           # active(running) 확인. 아니면: ollama serve
ollama pull qwen3:4b                              # 약 2.5GB
```

Ollama가 없으면 자동으로 템플릿 모드로 동작합니다(문장 다양성은 낮지만 흐름 동일).

### 자주 걸리는 문제

| 증상 | 해결 |
|---|---|
| `Command 'pip' not found` | 위 1번의 `apt install python3-venv python3-pip` |
| `externally-managed-environment` | venv 활성화(`source .venv/bin/activate`) 후 pip 실행 |
| Ollama 설치 중 `requires zstd` | `sudo apt install zstd` 후 재시도 |
| `gio: ... Operation not supported` | 무해함. WSL엔 브라우저가 없어서 자동 열기 실패 — Windows 브라우저에서 직접 접속 |
| 첫 실행 시 이메일 입력 프롬프트 | 그냥 Enter. 이 저장소의 `.streamlit/config.toml`이 이후엔 안 뜨게 함 |

## 사용법

### 검수 도우미 (권장)

```bash
source .venv/bin/activate
streamlit run app.py
```

Windows 브라우저에서 `http://localhost:8501` 접속.

1. **① 질문 생성** — 도메인·개수·배치 크기 선택 후 생성. 로컬 SQLite(`data/eval_assistant.db`)에 저장.
2. **② 검수 진행** — 질문 박스의 복사 버튼 → 카나나 앱에 붙여넣기 → 응답 전문 붙여넣기 후
   **E2E 품질평가 양식 그대로 2축 판정**: 1) 정확도(pass/fail + N 사유 + 코멘트),
   2) LLM 출력(pass/fail + 오류 유형 + 코멘트). search ID도 함께 기록.
   질문 자체가 이상하면 **"질문 결함 → 제외"** (생성 품질 개선 신호로 쓰임).
3. **③ 결과·내보내기** — 정확도/LLM 출력 pass율 집계, xlsx 다운로드.
   **'제출양식' 시트가 카카오 제출 컬럼과 동일**하고, '내부분석' 시트에 앱 응답 전문 등이 남는다.

### CLI만 쓰는 경우

```bash
python generate.py --count 100 --domain all --out data/questions.jsonl
python generate.py --count 30 --domain finance --mode template --seed 42   # 재현 가능
```

## 포트 정리

| 포트 | 서비스 | 용도 | 변경 방법 |
|---|---|---|---|
| 11434 | Ollama API | 앱 ↔ 모델 내부 통신 | `OLLAMA_HOST` 환경변수 / `generate.py --host` |
| 8501 | Streamlit | 브라우저에서 여는 검수 UI | `streamlit run app.py --server.port 8502` |

둘 다 기본값 관례일 뿐 고정이 아닙니다.

## 생성 속도가 느릴 때

CPU 추론에서 병목은 토큰 생성이 아니라 **호출마다 반복되는 프롬프트 처리(prefill)** 입니다.
이 도구는 다음이 기본 적용되어 있습니다:

- **배치 생성**: 호출 1회당 8개(조절 가능) 생성 → 고정 프롬프트 비용을 8분의 1로
- **keep_alive 30분**: 호출 간 모델 재로딩 방지 (첫 호출만 로딩 때문에 유독 느린 게 정상)
- **num_predict 상한**: 출력 폭주 방지

추가로 시도할 수 있는 것:

- **더 작은 모델**: `qwen3:1.7b` — 문장화만 하는 이 태스크에선 품질 하락이 작고 약 2배 빠름.
  모델 태그만 바꿔서 몇 배치 뽑아 비교해볼 것
- **WSL 자원 늘리기**: Windows의 `C:\Users\<이름>\.wslconfig`에
  `[wsl2]` `memory=12GB` `processors=8` 지정 후 PowerShell에서 `wsl --shutdown`.
  WSL 기본값은 호스트 RAM의 절반이라 CPU 추론 성능에 직접 영향
- **대량 생성은 걸어두기**: 대화형이 아니므로 점심/퇴근 전에 `generate.py`로 수백 건 걸어두는 운용이 합리적

## 파일 구성

| 파일 | 역할 |
|---|---|
| `taxonomy.yaml` | 도메인/인텐트/템플릿/스타일 축. **코드 수정 없이 여기만 고쳐서 확장** |
| `seeds.yaml` | 한국 특화 엔티티 풀 (지명·역·종목·금융용어). 스냅샷 기준일 명시 |
| `generate.py` | 슬롯 샘플링 → LLM 배치/템플릿 문장화 → 검증 → JSONL |
| `db.py` | 로컬 SQLite 저장소 (질문·검수 결과·few-shot 예시 풀) |
| `app.py` | Streamlit 검수 도우미 (생성/검수/집계·xlsx) |
| `.streamlit/config.toml` | 텔레메트리 차단, localhost 바인딩 등 팀 공통 설정 |

## 품질 관리 체계

생성 질문은 저장 전에 자동 검증을 통과해야 합니다: 엔티티 포함 여부, 길이,
**외국어 혼입(한자·가나·비고유명사 로마자)**, 완전/준중복. 탈락하면 재시도 후 템플릿 폴백.

여기에 사람 피드백 루프가 겹쳐집니다: 검수자가 "질문 결함 → 제외"로 표시한 질문은 few-shot에서
배제되고, **검수를 통과한 실제 질문이 few-shot 예시로 자동 재사용**되므로 검수가 쌓일수록
모델이 좋은 예시를 보고 생성하게 됩니다.

## 도메인·시드 확장하기

- **인텐트 추가**: `taxonomy.yaml`의 해당 도메인에 `id/name/slots/templates` 블록 추가.
- **새 슬롯**: `seeds.yaml`에 같은 이름의 풀을 추가. `{include: [풀A, 풀B]}`로 합성 풀 가능.
- **주의**: 인텐트와 슬롯 풀의 의미가 맞아야 함. 예: "계산" 인텐트에 `신용점수`가 들어가면
  비문이 생성됨 → 금융은 `fin_condition_term`/`fin_pay_term`/`fin_calc_term`처럼 용도별 풀 사용.
- **시드 갱신**: 종목명(KRX)·행정구역은 변동됨. 갱신 시 파일 상단 스냅샷 날짜도 수정.

## 데이터·보안 유의사항

- 모든 데이터(생성 질문, **카나나 앱 응답 전문**, 판정)는 이 PC의 `data/` 폴더에만 저장.
  유일한 네트워크 호출은 `localhost:11434`(Ollama)뿐.
- `.streamlit/config.toml`이 UI를 localhost로 제한함 — 기본값(0.0.0.0)이면 같은 네트워크의
  다른 PC에서 검수 데이터에 접근 가능하므로 이 설정을 지우지 말 것.
- 카나나 앱 응답은 고객사(카카오) 검수 데이터에 해당할 수 있음. `data/` 폴더와 추출 xlsx를
  개인 클라우드·외부 메신저로 옮기지 말고 사내 규정에 따른 경로로만 공유.

## 알려진 한계 / 다음 단계

- '기획 검수' 컬럼은 카카오 측 기입란으로 보고 빈 값으로 내보냄.
- 대시보드(추이·검수자별 현황·필터)는 2차 범위. `db.py`를 그대로 재사용해 붙일 수 있음.
- 템플릿 모드는 문형 다양성이 제한적 → 다양성이 필요한 배치는 LLM 모드 권장.
