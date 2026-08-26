# Dobby Code 지원서 주장 감사

**판정: 부분적으로 방어 가능.** Dobby의 실제 핵심은 durable DAG runtime, fail-closed artifact handoff, 분류 기반 복구, provider placement이다. 다만 일반 CLI가 사용자 요청을 AI로 동적 분해하는 것은 아니고, acceptance check가 0개여도 promotion되며, 인간-AI 역할 분리 전체가 코드로 강제되는 것도 아니다.

감사 원칙: README·주석은 판정 근거에서 제외했다. 아래 근거는 실행 코드, 심볼, 테스트와 이번 세션 실행 결과다. 경로 뒤 숫자는 현재 파일의 관련 줄이다.

## 1. 사실 검증표

| 주장 | 판정 | 실제 동작 | 근거 파일 | 함수·클래스 | 관련 테스트 | 지원서에서 안전하게 말할 수 있는 표현 | 과장되는 표현 |
|---|---|---|---|---|---|---|---|
| 1 | 부분 구현 | provider/command/static/judge worker를 같은 TaskGraph/contract/retry/store 위에서 실행하는 범용 실행 커널이다. 그러나 일반 CLI의 graph shape는 고정이고 모든 agent 업무를 자동 설계하는 범용 planner는 아니다. | `dobby/runtime/runner.py:100-206,766-834`; `dobby/runtime/workers.py:244-529` | `Runner`, `default_graph`, `WorkerRegistry`, `ProviderWorker` | `RunnerBehaviour.test_a_four_node_run_completes_and_promotes_every_artifact`; `WorkerContracts.*` | “여러 worker와 provider를 공통 contract로 실행하는 AI agent runtime harness를 구현했다.” | “어떤 목표든 스스로 분해·수행하는 완전자율 범용 agent다.” |
| 2 | 부분 구현 | Context retrieval은 `dobby context`, routing은 agency/budget 산정, runtime은 고정 DAG 또는 accepted project plan의 compiled DAG를 실행한다. 검증·failure 기록·resume은 구현됐다. 일반 `runtime run`의 plan artifact는 graph를 다시 쓰지 않는다. | `dobby/cli.py:1545-1579`; `dobby/runtime/runner.py:766-834`; `dobby/project/workorder.py:166-379` | `cmd_runtime`, `default_graph`, `compile_orders`, `compile_graph` | `TestCompileGraph.*` (`tests/test_workorder.py`); `RunnerBehaviour.*` | “목표를 실행 graph와 contract로 만들고 실행·검증·복구·resume을 관리했다.” | “매 요청마다 AI가 context/tool을 모아 동적으로 업무를 분해·재계획한다.” |
| 3 | 완전 구현 | role hard constraint→가용성/quota→명시 override→측정 utility 또는 static preference 순으로 provider를 고른다. CLI 탐지 결과 agy/claude/codex/gemini가 usable이었다. | `dobby/providers/policy.py:89-168`; `dobby/runtime/placement.py:210-444`; `dobby/runtime/runner.py:577-619` | `ROLE_POLICY`, `admissible`, `ProviderPlacement.choose`, `Runner._place` | `AFocusedPatchGoesToCodex.*`; `ABroadTaskGoesToAgyInsideIsolation.*`; `Scoring.*` | “역할·격리·쓰기 권한·quota와 누적 성공률/지연/비용 tier를 기준으로 provider를 배치한다.” | “항상 최적 모델을 자동 선택한다” 또는 “Gemini가 모든 mechanical task를 실제 수행한다.” |
| 4 | 완전 구현(정상 Runner 경로) | worker 결과를 PROPOSED로 만들고 verifier를 통과하면 코드상 연속 전이해 PROMOTED한다. 실패하면 REJECTED다. VERIFIED는 DB에 독립 중간 상태로 오래 남지 않는다. | `dobby/runtime/contracts.py:65-77,248-283`; `dobby/runtime/runner.py:486-525` | `Artifact.transition`, `Artifact.usable`, `Runner._store_artifact` | `VerifierGate.test_passing_checks_promote`; `RunnerBehaviour.test_an_unverified_artifact_never_reaches_the_next_node` | “산출물 상태를 구분하고, Runner가 검증 성공 후에만 downstream용 PROMOTED로 전이한다.” | “VERIFIED 상태에서 별도 인간 승인 후 PROMOTED된다.” |
| 5 | 부분 구현 | schema, grounding, 선언된 acceptance checks가 모두 통과하고 `not_run`이 없어야 promotion된다. 단 checks가 빈 목록이면 schema만으로 통과하며 free-form schema도 가능하다. 저수준 store API에는 promotion 재검증이 없다. | `dobby/runtime/verify.py:98-158,342-354`; `dobby/runtime/runner.py:639-666`; `dobby/runtime/store.py:667-693` | `Verifier.verify`, `promotable`, `Runner._promoted_inputs`, `RunStore.put_artifact` | `test_a_check_that_cannot_run_here_blocks_promotion`; `test_a_rejected_artifact_never_reaches_the_dependent` | “선언된 schema/grounding/check가 실패하거나 실행 불가하면 Runner가 promotion과 전달을 차단한다.” | “모든 artifact는 반드시 하나 이상의 acceptance check를 실행해야 하며 어떤 API로도 우회 불가하다.” |
| 6 | 완전 구현(조건부) | SQLite에 run/node/attempt/artifact/effect/span과 append-only event를 저장한다. resume은 같은 `Runner.run`이 projection을 reload하고 terminal node를 제외한다. 열린 attempt는 lease 소유자가 죽었거나 만료됐을 때만 READY로 복구된다. | `dobby/runtime/store.py:75-178,402-426,544-616`; `dobby/runtime/runner.py:155-177,248-336`; `dobby/runtime/graph.py:208-248` | `RunStore`, `Runner._reconcile`, `TaskGraph.ready_nodes` | `test_work_that_finished_before_the_kill_is_not_repeated`; `test_a_finished_run_is_not_restarted_by_a_second_call`; `RecoveryRespectsALiveHolder.*` | “동일 run_id의 terminal node는 resume 시 재실행하지 않고, 중단된 open attempt만 lease 조건에 따라 복구한다.” | “어떤 재실행에서도 이미 한 작업을 절대 반복하지 않는다.” |
| 7 | 완전 구현 | 8개 failure class가 5개 action으로 매핑된다. RETRY_SAME/ELSEWHERE/REPAIR는 attempt 한도까지, WAIT는 approval block, FAIL은 terminal이다. `REPAIR`는 failure를 다음 prompt context에 넣는다. | `dobby/runtime/failures.py:29-117,181-252`; `dobby/runtime/runner.py:527-574` | `DEFAULT_POLICY`, classifiers, `Runner._fail_attempt` | `FailureClassification.*`; `test_a_contract_violation_repairs_with_the_failure_in_hand`; `test_retry_elsewhere_actually_moves_the_call` | “오류를 분류해 동일 provider 재시도·provider 전환·repair·human wait·fail을 다르게 적용한다.” | “AI가 원인을 깊이 추론해 항상 올바른 복구 전략을 만든다.” |
| 8 | 완전 구현(메커니즘 가치) | downstream input 조회가 `state=PROMOTED`로 고정되고 dependency도 SUCCEEDED여야 READY다. 이 문장은 구현의 중심을 정확히 요약한다. 단 저수준 DB/API를 신뢰 경계 밖 공격자로부터 보호하는 보안 모델은 아니다. | `dobby/runtime/graph.py:208-224`; `dobby/runtime/runner.py:639-666` | `TaskGraph.ready_nodes`, `Runner._promoted_inputs` | `test_an_unverified_artifact_never_reaches_the_next_node`; `test_a_rejected_artifact_never_reaches_the_dependent` | “AI 결과의 downstream 사용 자격을 deterministic gate와 상태 전이로 통제했다.” | “AI 결과의 의미적 진실까지 자동 보장한다.” |
| 9 | 부분 구현 | AI는 provider worker/architect/judge로 생성·제안을 맡을 수 있다. 인간은 최초 task, CLI options, acceptance checks, irreversible approval, 불명확한 external effect reconciliation을 맡는다. 그러나 validation 실행과 promotion은 코드가 하고, 최종 채택에 인간 승인이 항상 필요한 것은 아니다. | `dobby/cli.py:1545-1579`; `dobby/runtime/scheduler.py:184-220`; `dobby/project/architecture.py:564-671`; `dobby/project/session.py:183-241` | `Scheduler.next_nodes`, `request_architecture`, `promote_from_run` | `test_an_irreversible_node_does_not_run_without_an_approval`; `test_architect_cannot_drop_existing_checks` 계열 | “AI에 생성·탐색을 맡기되 사람이 목표와 gate를 설정한다는 운영 철학을, 일부 approval/contract 경계로 구현했다.” | “사람이 모든 검증과 PROMOTED/최종 채택을 직접 결정하도록 코드가 강제한다.” |

## 2. 실제 실행 흐름

일반 `dobby runtime run`의 실제 흐름이다. Project의 accepted plan 경로는 2단계에서 `compile_graph`가 중간 node를 추가한다.

| 단계 | 결정자 | 입력 | 출력 | 검증방법 | 다음 조건 |
|---|---|---|---|---|---|
| 1. CLI 요청/옵션 | Human | task, `--provider`/`--execute`, `--check`, budget/approval 옵션 | task와 실행 설정 | argparse/필수값 검사 | graph 생성 가능 |
| 2. Graph 생성 | Deterministic Code | task와 옵션 | 고정 plan→execute→verify→report `TaskGraph` | DAG의 unknown dependency/cycle/duplicate 검사 (`TaskGraph._validate`) | 유효 graph |
| 3. Context routing | Deterministic Code | task, KG/policy/skill registry | agency level, model tier, policies, skills, budget | `Router.route`; 결과는 run metadata/budget | route 생략 옵션이 아니면 기록 |
| 4. Run 저장 | Deterministic Code | task, graph, budget, route | SQLite run/node/event rows | transaction과 PK | run_id 생성 |
| 5. Node admission | Deterministic Code | persisted graph, dependency state, approval, budget | 실행할 ready nodes | dependency=SUCCEEDED, irreversible approval, budget reserve | lease 획득 |
| 6. Provider placement | Deterministic Code | node role/kind, fleet, policy, quota, scorecard, override | provider 또는 거부 사유 | hard constraints 후 measured utility/static preference | provider가 selectable |
| 7. Task 실행 | AI 또는 Tool | instruction, promoted dependency payload만, repair context | worker payload/meta 또는 classified failure | adapter exit/output/effect observation | 성공이면 verifier; 실패면 recovery |
| 8. Artifact 검증 | Deterministic Code | payload, schema, grounding, acceptance checks | `VerifierResult` | shape→grounding→commands; 실행불가는 fail | `passed && !not_run` |
| 9. 상태 전이/전달 | Deterministic Code | verdict와 PROPOSED artifact | REJECTED 또는 VERIFIED→PROMOTED; node SUCCEEDED | `Artifact.transition`, `promotable` | dependents가 ready |
| 10. 복구/종료/Resume | Deterministic Code + 일부 Human | failure class, attempt, lease/effect/approval state | retry/repair/elsewhere/wait/fail; persisted state/report | policy attempt limit; all nodes succeeded/done/deferred | terminal이면 종료, WAITING이면 같은 run_id resume |

없는 단계: 일반 CLI에서 AI plan 결과를 읽어 graph를 재작성하는 동적 replanning. `plan` node는 artifact를 만들지만 graph는 이미 만들어져 있다. Project architect의 `execution_steps`는 opt-in이고 accepted plan을 `compile_graph`가 제한된 role/scout/implement/critic 구조로 변환한다.

## 3. 인간과 AI의 책임 경계

| 판단 | 주체 | 이유 |
|---|---|---|
| 문제/목표 정의 | Human | CLI task와 project outcome은 외부 입력이다. |
| Scope 설정 | Hybrid | 인간/manifest가 root·checks를 정하고, workorder compiler가 path/root·role 제한을 강제한다. |
| Task 분해 | Hybrid | 일반 runtime은 코드의 고정 4-node graph; project architect는 AI가 `execution_steps`를 제안하고 compiler가 제한·변환한다. |
| Provider 선택 | Deterministic Code | role policy와 placement score가 선택한다. 인간 override는 policy를 우회하지 못한다. |
| 대안 생성 | AI | panel/protocol 또는 provider output이 대안을 만든다. 호출 여부와 protocol은 인간/코드가 정한다. |
| Acceptance Criteria 설정 | Hybrid | CLI/project item은 인간 입력. Architect가 allow-listed smoke check를 추가할 수 있으나 기존 check 삭제·임의 command 추가는 거부된다. |
| 코드/산출물 생성 | AI / Tool | provider worker 또는 deterministic command worker가 생성한다. |
| Validation 실행 | Deterministic Code | `Verifier.verify`가 schema/grounding/check를 실행한다. |
| VERIFIED 판정 | Deterministic Code | `promotable` 성공 직후 `Artifact.transition(VERIFIED)`. |
| PROMOTED 판정 | Deterministic Code | 정상 Runner에서 VERIFIED 직후 자동 transition; 별도 인간 결정 없음. |
| 실패 원인 분류 | Deterministic Code | marker/exit/schema/check 결과 기반 classifier다. 의미적 root-cause AI 분석은 아니다. |
| Retry 여부 | Deterministic Code | `DEFAULT_POLICY`와 node attempt 수가 결정한다. |
| Provider 전환 | Deterministic Code | CAPACITY/일부 contract failure의 avoid flag와 placement가 결정한다. |
| Human approval 조건 | Deterministic Code + Human | EXTERNAL_IRREVERSIBLE, provider policy refusal, unresolved external effect는 코드가 block하고 인간이 승인/확인/해제한다. |
| 최종 결과 채택 | Deterministic Code(기본), Human(운영) | runtime은 all-node success로 자동 SUCCEEDED; project item도 `promote_from_run`이 자동 DONE 처리한다. 인간 최종 채택은 코드의 보편 invariant가 아니다. |

지원서에서의 정확한 분리: **AI에 맡긴 것**은 plan/patch/report/critique 등의 후보 산출물 생성이다. **직접 설계·판단한 것**은 task/범위/acceptance contract, side-effect/role policy, failure-action mapping, 그리고 결과를 채택할 운영 판단이다. **코드가 대신 판정한 것**은 check 실행, artifact promotion, retry와 resume이다.

## 4. 검증 Gate 상세

정상 경로는 `Runner._run_attempt` (`dobby/runtime/runner.py:486-525`)이다.

1. `_store_artifact`가 기본 상태 PROPOSED인 `Artifact`를 만든다.
2. `Verifier.verify`가 schema → grounding → 모든 선언 check 순서로 실행한다.
3. `promotable`은 `verdict.passed and not verdict.not_run`일 때만 true다.
4. false면 `PROPOSED→REJECTED`, failure policy로 이동한다.
5. true면 한 코드 블록에서 `PROPOSED→VERIFIED→PROMOTED` 후 파일·DB를 갱신하고 node를 SUCCEEDED로 만든다.

세부 판정:

- VERIFIED 조건: schema 위반 없음, grounding 실패 없음, 선언된 checks 모두 pass, 실행불가 check 없음.
- PROMOTED 조건: VERIFIED와 사실상 동일하다. 별도 승인·quality threshold는 없고 바로 연속 전이한다.
- check가 하나라도 **선언됐지만 실행되지 않음**: `not_run` 때문에 차단. `VerifierGate.test_a_check_that_cannot_run_here_blocks_promotion`이 검증한다.
- check가 **아예 0개**: 빈 records가 pass이므로 schema만 맞으면 promotion. `default_graph`의 plan/execute/report가 흔히 이 경로다. “반드시 acceptance check를 하나 이상 실행”은 거짓이다.
- 실패 artifact downstream: 정상 Runner에서는 dependency가 SUCCEEDED가 아니면 ready가 아니며, `_promoted_inputs`가 DB에서 PROMOTED만 조회한다. `test_an_unverified_artifact_never_reaches_the_next_node`, injection의 `test_a_rejected_artifact_never_reaches_the_dependent`가 검증한다.
- 우회: `Artifact(state=PROMOTED)` 생성 자체와 `RunStore.put_artifact`는 verifier 증거를 재검사하지 않는다. `set_node_state(..., enforce=False)`도 내부 복구용으로 공개돼 있다. 따라서 동일 프로세스의 신뢰받는 runtime kernel에서는 invariant지만 악성/오용 저수준 API에 대한 security boundary는 아니다. 이 우회 자체를 거부하는 테스트는 없다.
- VERIFIED 지속성: DB에는 최종 PROMOTED/REJECTED만 보통 보이며 VERIFIED는 독립 검토 queue가 아니다.

## 5. 실패 및 복구

실제 `DEFAULT_POLICY` 전체다.

| Failure class | 실제 발생 예 | Action | Same Provider | Different Provider | Wait | Human | 최종 Fail 조건 |
|---|---|---|---|---|---|---|---|
| TRANSIENT_PROVIDER | timeout/temporary network marker; command timeout도 현재 이 class | RETRY_SAME, max 3, exponential backoff | 예 | 아니오 | 시간 backoff | 아니오 | 3번째 실패 |
| CAPACITY | 429/rate limit, placement 없음, queue timeout | RETRY_ELSEWHERE, max 3, 5s base, last provider avoid | 아니오 | 예 | backoff | 아니오 | 대안 없음/3번째 실패 |
| CONTRACT_VIOLATION | empty output, schema mismatch | REPAIR, max 2, last provider avoid | 보통 아니오 | 가능 | 없음 | 아니오 | 2번째 실패 |
| QUALITY_FAILURE | acceptance/grounding 실패, command nonzero | REPAIR, max 2 | 예 가능 | 기본 avoid 아님 | 없음 | 아니오 | 2번째 실패 |
| POLICY_BLOCKED | provider permission refusal, unresolved effect | WAIT | 아니오 | 아니오 | approval state | 예 | 자동 fail 아님; 해결/승인 필요 |
| NON_RETRYABLE | auth, unknown error, shell 126/127, worker exception | FAIL | 아니오 | 아니오 | 아니오 | 운영자가 설정 수정 | 즉시 |
| PERMISSION_DENIED | node가 요구한 write와 grant 불일치 | FAIL | 아니오 | 아니오 | 아니오 | 구성 변경 필요 | 즉시 |
| EFFECT_NOT_OBSERVED | write를 선언했으나 변화 없음 | REPAIR, max 2 | 예 가능 | 기본 avoid 아님 | 없음 | 아니오 | 2번째 실패 |

중요한 비판: “Failure를 먼저 분류”는 맞지만 classifier는 대부분 문자열 marker·exit code·verifier 결과의 deterministic rule이다. AI root-cause analysis가 아니다. `WAIT`는 일반적인 capacity wait action이 아니라 human approval/effect reconciliation block이다.

## 6. Resume/재현성

SQLite schema (`dobby/runtime/store.py:75-178`)는 다음을 저장한다.

- `events`: append-only run/node/attempt event와 payload
- `runs`: task, state, budget, route, repo, timestamps
- `nodes`: immutable serialized spec + authoritative state/attempt count + lease owner/expiry
- `attempts`: start/finish/outcome/failure class/detail/worker, `(run,node,attempt)` PK
- `artifacts`: id, node, kind, state, digest, path
- `spans`: trace timing/attributes
- `effects`: identity-derived idempotency key와 confirmation digest

Resume은 별도 알고리즘이 아니라 같은 `Runner.run`이다. `load_run`이 node projection state를 authoritative하게 복원하고, `ready_nodes`는 PENDING/READY만 선택한다. SUCCEEDED/FAILED/SKIPPED는 terminal이므로 재실행되지 않는다. 전체 run이 terminal이면 즉시 report한다.

정확한 조건:

- **재실행하지 않음**: 같은 run_id, node state가 terminal이고 store가 온전할 때. external effect가 CONFIRMED이면 idempotent no-op으로 완료한다.
- **재실행함**: open STARTED attempt의 lease가 만료됐거나 holder가 죽은 것으로 확인돼 READY로 복구될 때. 이는 “완료되지 않은” attempt다.
- **재실행하지 않고 block**: external effect가 CLAIMED지만 CONFIRMED가 아니면 중복 위험 때문에 human/effect-provider 확인을 기다린다.
- 보장 밖: 새 run_id, 수동 DB 변경, 비-idempotent LOCAL_WRITE worker의 외부 숨은 효과, 저수준 API 오용.

테스트: `AKilledProcess.test_work_that_finished_before_the_kill_is_not_repeated`, `test_a_process_killed_mid_node_leaves_a_recoverable_run`, `RecoveryRespectsALiveHolder.*`, `EffectReconciliation.*`.

## 7. Multi-Agent / Multi-Provider

판정: **“복수 Agent를 역할과 작업 특성에 따라 배치하도록 설계하고 구현했다”는 방어 가능하되, 조건을 붙여야 한다.**

- Router: `core.router.Router`는 agency level/model tier/budget/policy/skill을 산정하지만 runtime provider를 직접 선택하지 않는다.
- Runtime placement: `ProviderPlacement.choose`가 node role/kind에 따라 `ROLE_POLICY` 후보를 좁히고 격리, write grant, quota, circuit breaker를 적용한다. 측정 history가 있으면 verified success proxy, p95 latency, cost tier, consecutive failure의 weighted utility를 쓴다. 없으면 static subscription-first preference다.
- 사용자 지정: node config의 provider와 CLI override가 가능하지만 policy/quota/isolation을 우회하지 못한다. 일반 `runtime run --provider`는 graph worker를 지정하지만 실행 시 placement가 다시 role policy를 적용한다.
- 실제 역할: implement 기본 후보 codex→agy(격리 필요)→claude; architect claude→codex→gemini; critic/scout는 codex/agy/claude/gemini 후보. 이것은 “각 제품이 언제나 그 역할을 수행했다”는 실사용 실적이 아니라 executable policy다.
- 비용/품질/latency: 사용한다. 다만 cost는 catalog tier 정규화이고 provider별 실제 USD 측정은 제한적이다. 얇은 sample은 provisional이다.
- Panel/Swarm: `swarm.topologies`와 `swarm.protocols`는 역할/lens/dependency **계획**과 다양성 측정이다. 파일 자체는 agent를 spawn하지 않으며 `providers/fanout.py`가 실행한다. Runtime placement는 한 TaskNode를 어느 provider에 보낼지 정하는 별도 계층이다.

테스트: `AFocusedPatchGoesToCodex`, `ABroadTaskGoesToAgyInsideIsolation`, `AFailedProviderFallsBackWithinThePolicy`, `TheClaudeQuotaIsNotATieBreak`, `Scoring`, `TestProtocols`, `TestDiversityVerdicts`.

## 8. 실제 사용 예시

“Python 데이터 분석 코드의 오류를 수정하고 `python -m unittest ...`로 검증”을 `dobby runtime run`에 provider와 check로 넣는 실제 가능한 시나리오다.

1. 사람이 task, provider 사용 의도, acceptance command를 CLI에 입력한다.
2. 코드는 고정 plan→execute→verify→report DAG와 budget/route metadata를 저장한다.
3. plan node가 provider에 계획 JSON을 요청하고 schema가 맞으면 promotion한다. 이 계획이 DAG를 재작성하지는 않는다.
4. execute node는 promoted plan만 context로 받고 코드를 수정한다.
5. side-effect observer와 output contract가 성공을 확인한다.
6. verify node의 contract가 사람이 선언한 unittest command를 실제 실행한다.
7. check 성공 시 verify artifact가 PROMOTED되어 report node가 실행된다.
8. check 실패 시 QUALITY_FAILURE→failure output을 포함한 REPAIR attempt를 최대 한 번 더 실행한다.
9. provider capacity면 last provider를 피하고 다른 eligible provider를 고른다. auth/unknown permanent error면 즉시 fail한다.
10. 중단되면 같은 run_id로 resume하고 terminal node는 건너뛰며 open attempt만 lease/effect 상태에 따라 복구한다.

가상 기능을 피하기 위한 제한: 일반 runtime의 plan은 execute graph를 동적으로 세분화하지 않는다. 더 세분화된 scout/implement/critic graph는 project architect plan이 수락되고 `compile_graph`를 사용한 경우에만 가능하다.

## 9. 내 AI 활용 철학과의 관계

**선택: B. 일부만 구현하고 나머지는 사용자의 운영원칙이다.**

코드 근거가 있는 부분:

- AI/provider worker에 plan·실행 산출물·report·critic 의견을 맡길 수 있다.
- 인간이 task와 CLI acceptance checks를 제공한다.
- irreversible action과 불명확한 external effect는 human approval/reconciliation을 요구한다.
- deterministic code가 schema/check/promotion/retry/resume을 판정한다.
- architect가 acceptance를 제안해도 allow-list 밖 command, 기존 check 삭제, 위험 확대는 거부한다.

운영 철학인 부분:

- “탐색은 AI, 최종 채택은 항상 인간”은 runtime invariant가 아니다. runtime/project는 조건 충족 시 자동 SUCCEEDED/DONE/PROMOTED한다.
- “사람이 검증한다”도 문자 그대로는 틀리다. 사람은 검증 기준을 정할 수 있지만 검증 실행과 판정은 코드다.
- “빠르게 넓게”는 panel/swarm 사용 시 가능한 전략이지 모든 runtime run의 동작이 아니다.

가장 정확한 문장: **“AI에는 탐색·산출물 생성을 맡기고, 사람은 목표·범위·acceptance 기준과 위험 승인에 책임을 지며, deterministic runtime이 검증·promotion·복구를 집행하도록 설계했다.”**

## 10. 지원서 문구 감사

### A. 코드가 100% 뒷받침하는 매우 보수적인 표현

AI·명령 worker를 DAG로 실행하고, 산출물을 PROPOSED/REJECTED/PROMOTED로 관리하는 runtime을 구현했습니다. 선언된 schema·grounding·acceptance command가 실패하거나 실행되지 않으면 downstream 전달을 차단하고, 실행 상태·attempt·event를 SQLite에 저장해 동일 run을 재개합니다.

### B. 기술면접에서 충분히 방어 가능한 가장 강한 표현

Dobby를 AI 결과의 사용 자격을 통제하는 agent harness로 설계했습니다. 역할·격리·권한과 누적 성공/지연 지표로 provider를 배치하고, 검증 실패를 분류해 retry·repair·provider 전환·human wait로 연결했습니다. terminal task는 resume 시 건너뛰고 PROMOTED artifact만 후속 node에 전달합니다.

### C. 과장이라서 사용하면 안 되는 표현

Dobby는 모든 복합 목표를 AI가 자동 분해하고 최적 모델에 배치하며, 모든 결과에 반드시 acceptance test와 사람의 승인을 거쳐 진실성을 보장합니다. 어떤 API나 재시작 상황에서도 실패 산출물 유입·중복 실행이 절대 불가능하고, 사람만 최종 PROMOTED를 결정합니다.

## 11. 면접 대비

### Q1. “VERIFIED와 PROMOTED는 실제로 무엇이 다른가?”

핵심 답변: 상태 전이상 구분되지만 현재 Runner는 `artifact.transition(VERIFIED).transition(PROMOTED)`를 연속 호출한다 (`runner.py:515`). 별도 인간 승인/queue는 없다. VERIFIED는 개념적 중간 상태이고 운영상 독립 단계라고 말하면 과장이다.

### Q2. “Acceptance check를 안 쓰면 promotion이 막히나?”

핵심 답변: 아니다. 빈 list는 모두 통과한 것으로 처리된다 (`verify.py:135-148`). schema도 비어 있으면 payload가 사실상 무검증 promotion될 수 있다. 선언된 check의 실행불가는 `promotable`이 막는다 (`verify.py:342-354`).

### Q3. “실패 artifact가 절대로 downstream에 못 들어간다는 보장의 경계는?”

핵심 답변: 정상 Runner 경로에서는 dependency SUCCEEDED와 DB `state=PROMOTED` 조회의 이중 조건이다 (`graph.py:208-224`, `runner.py:639-666`). 그러나 `RunStore.put_artifact`는 임의 PROMOTED artifact를 재검증하지 않으므로 저수준 API/DB는 신뢰 경계 안이다.

### Q4. “AI가 task를 어떻게 분해하는가?”

핵심 답변: 일반 runtime은 분해하지 않고 고정 4-node graph다 (`default_graph`). Project architect가 opt-in으로 `execution_steps`를 제안하면 `compile_orders/compile_graph`가 scout/implement/critic을 제한적으로 컴파일한다. plan node 결과가 runtime graph를 동적으로 바꾸지는 않는다.

### Q5. “Provider 선택이 정말 지표 기반인가, 하드코딩인가?”

핵심 답변: 둘 다다. role/isolation/write/quota는 hard rule, 기록이 있으면 success/p95/cost tier/reliability weighted utility, 없으면 static preference다 (`placement.py:210-444`). “항상 metric 최적화”는 거짓이다.

### Q6. “Retry와 repair의 코드상 차이는?”

핵심 답변: RETRY_SAME은 동일 조건 재호출, RETRY_ELSEWHERE는 last provider를 avoid list에 추가, REPAIR는 `repair_context`에 classified failure를 넣어 다음 instruction을 바꾼다 (`runner.py:538-574`).

### Q7. “완료 task를 다시 실행하지 않는 정확한 메커니즘은?”

핵심 답변: SQLite node projection의 terminal state, attempt PK, lease reconciliation이다. `ready_nodes`는 PENDING/READY만 반환하고 terminal run은 즉시 report한다. 단 새 run_id나 수동 상태 변조에는 적용되지 않는다.

### Q8. “중단 직전 외부 API 호출이 성공했는지 모르면?”

핵심 답변: effect를 실행 전에 identity key로 CLAIM하고 성공 후 CONFIRM한다. CLAIMED-only 상태는 반복도 성공 처리도 하지 않고 BLOCKED_ON_APPROVAL로 보내 인간/외부 조회가 confirm/release하도록 한다 (`runner.py:391-411`, `store.py:696-738`).

### Q9. “사람이 최종 결과를 채택한다는 것이 코드로 강제되나?”

핵심 답변: 보편적으로 아니다. irreversible/effect ambiguity에는 human gate가 있지만 정상 artifact는 자동 PROMOTED, 성공 run은 자동 SUCCEEDED, project item은 `promote_from_run`이 자동 DONE으로 만든다. 인간 최종 채택은 운영 원칙이다.

### Q10. “테스트로 어디까지 입증했나?”

핵심 답변: 이번 세션에서 runtime/injection/lease/placement/provider/swarm/orchestration 8개 모듈 268개를 `unittest`로 실행해 OK였다. 전체 discover는 120초·300초·600초 모두 timeout이라 전체 green은 입증하지 못했다. 핵심 invariant 테스트명은 본 보고서 각 절에 명시했다.

## 실행 증거와 한계

| 명령 | 결과 | 해석 |
|---|---|---|
| `python -m dobby.cli doctor` | all checks pass; 4 usable providers 탐지 | 환경/CLI 탐지 성공. 실제 provider 호출 품질 증거는 아님. |
| 핵심 8개 test module `python -m unittest -q ...` | `Ran 268 tests ... OK` | 감사 핵심 범위의 구현 테스트 통과. |
| `python -m unittest discover -s tests -q` | 120s, 300s, 600s timeout | 전체 suite pass/fail 미확정. timeout을 green으로 해석하지 않음. |
| `python -m pytest ...` | `No module named pytest` | doctor가 pytest 부재를 검출하지 못함. 표준 unittest로 대체. |
| `python -m dobby.cli review --reviewers 3 --risk correctness,security,reproducibility,contract ...` | review plan 생성; security/functional_suitability/reliability 3/7 관점 배정 | 실제 독립 reviewer 실행 결과가 아니라 review coverage 계획이다. performance/compatibility/maintainability/testability 및 요청한 correctness/reproducibility/contract는 uncovered/unknown으로 보고됐다. |

미검증/미완료: 실제 유료 provider 호출을 통한 end-to-end quality 비교, 전체 suite green, 악성 DB/API caller에 대한 security invariant, 사람의 실제 운영 습관은 이 감사로 입증하지 않았다.

**이 저장소의 실제 핵심은 검증된 artifact만 durable task graph의 다음 판단으로 전달하는 상태·계약·복구 메커니즘이다.**
