# 3팔 회귀 실험: 결과

`reports/LEDGER_three_arm_regression.md`이 **호출 한 번 하기 전에** 적어둔
임계값에 대한 답. 2026-08-26.

## 결론 먼저

선언한 가설은 **반박됐다.** 그리고 회귀를 낸 유일한 팔은 dobby였다.

> 선언: 단일 호출 10회(5 인스턴스 x 솔로 2팔) 중 **둘 이상**이 타겟 테스트를
> 통과시키면서 회귀를 하나 이상 낳으면 지지, **0 또는 1**이면 반박.

측정: **0회.** claude와 codex는 다섯 인스턴스에서 한 번도 회귀를 내지 않았다.
`django__django-11532`에서 codex가 다섯 개를 깨뜨렸던 파일럿은 일반화되지
않는다. n=1은 일화였고, n=10은 그 일화를 지지하지 않는다.

회귀 4건은 전부 `django__django-11138`의 `D_dobby`에서 나왔다. 게이트가 잡으라고
만든 실패를 게이트를 단 팔이 냈고, 게이트는 잡지 못했다.

```
timezones.tests.NewDatabaseTests.test_query_datetime_lookups
timezones.tests.NewDatabaseTests.test_query_datetime_lookups_in_other_timezone
timezones.tests.NewDatabaseTests.test_query_datetimes
timezones.tests.NewDatabaseTests.test_query_datetimes_in_other_timezone
```

## 팔별

| arm | resolved | F2P | 회귀 | 토큰 | 초 | 계측 완전 |
|---|---|---|---|---|---|---|
| `A_claude` (claude 1콜) | 4/5 | 9/10 | 0 | 6,770,951 | 1383 | 5/5 |
| `B_codex` (codex 1콜) | 3/5 | 7/10 | 0 | 3,544,621 | 846 | 5/5 |
| `D_dobby` (dobby 루프) | 1/5 | 6/10 | 4 | 1,122,506 | 1672 | 1/5 |

dobby는 다섯 중 하나를 풀었다. claude는 넷, codex는 셋이다. 이 코퍼스에서 dobby는
두 솔로 팔보다 **나쁘다**, 시간도 더 쓰고.

## 인스턴스별

| instance | arm | resolved | FAIL_TO_PASS | 회귀 | 호출 | 토큰 | 초 |
|---|---|---|---|---|---|---|---|
| `dj-11138` | `A_claude` | **yes** | 1/1 | 0 | 1 | 1,569,138 | 296 |
| `dj-11138` | `B_codex` | **yes** | 1/1 | 0 | 1 | 1,081,222 | 220 |
| `dj-11138` | `D_dobby` | no | 0/1 | **4** | 5 | 225,472 | 395 |
| `dj-11400` | `A_claude` | **yes** | 3/3 | 0 | 1 | 861,698 | 143 |
| `dj-11400` | `B_codex` | no | 1/3 | 0 | 1 | 330,076 | 98 |
| `dj-11400` | `D_dobby` | **yes** | 3/3 | 0 | 5 | 218,730 | 344 |
| `dj-11734` | `A_claude` | no | 0/1 | 0 | 1 | 1,268,222 | 406 |
| `dj-11734` | `B_codex` | no | 0/1 | 0 | 1 | 566,977 | 125 |
| `dj-11734` | `D_dobby` | no | 0/1 | 0 | 5 | 293,516 | 482 |
| `dj-13121` | `A_claude` | **yes** | 1/1 | 0 | 1 | 1,964,703 | 393 |
| `dj-13121` | `B_codex` | **yes** | 1/1 | 0 | 1 | 800,146 | 225 |
| `dj-13121` | `D_dobby` | no | 0/1 | 0 | 1 | 171,087 | 150 |
| `dj-13195` | `A_claude` | **yes** | 4/4 | 0 | 1 | 1,107,190 | 146 |
| `dj-13195` | `B_codex` | **yes** | 4/4 | 0 | 1 | 766,200 | 179 |
| `dj-13195` | `D_dobby` | no | 3/4 | 0 | 5 | 213,701 | 299 |

## 토큰 수치를 그대로 읽으면 안 되는 이유

`D_dobby`의 1,122,506은 **바닥값이지 측정값이 아니다.** 다섯 인스턴스 중 넷에서
5개 호출 중 3개가 usage를 보고하지 않았고, 하네스가 그렇게 적어놨다:

> `3 of 5 call(s) reported no usage; every total here is a FLOOR, not a
> measurement`

계측 완전 열이 `1/5`인 게 그 뜻이다. 그래서 "dobby가 토큰을 6분의 1 쓴다"는
말은 이 데이터로 할 수 없다. 진짜 값은 1,122,506보다 크고, 얼마나 큰지는 여기서
모른다.

원인은 `dobby/runtime/runner.py:467`이다:

```python
"collect_usage": self.quota is not None,
```

usage 수집은 CLI의 argv를 바꾸므로 읽을 원장이 있을 때만 켠다 — 그 자체는 맞는
결정이고 주석이 이유를 적어놨다. 놓친 건 **평가 하네스도 읽는 쪽이라는 것**이다.
원장을 끄고 돌리면 claude는 여전히 계측된다(`claude --output-format json`이
기본 경로라서). codex는 `codex exec --json`이 이 플래그 뒤에 있어서 통째로
사라진다. 결과적으로 계측 누락이 **provider마다 다르게** 생기고, 그러면 팔 간
토큰 비교가 성립하지 않는다.

솔로 `B_codex`는 같은 codex를 쓰면서 5/5 계측됐다. 같은 provider, 두 경로, 한쪽만
보인다.

이건 이번 원장이 범위로 잡은 행이 아니라서 고치지 않고 발견으로 남긴다(규칙 5).
고치면 이 실험의 수치가 바뀐다. 바뀐 수치를 얻으려면 provider 호출을 다시
사야 한다.

## 실행 중 고친 것

**`evals/swebench/score_one.py`** — 출력 파일을 arm 이름만으로 키를 잡는다.
인스턴스를 하나 더 채점할 때마다 앞의 행을 덮어썼다. 15번 채점하고 파일에는 3행이
남았고 전부 마지막 인스턴스였다. 콘솔은 맞고 기록이 틀렸다. 둘 중 나쁜 쪽이다. `f"{instance_id}::{arm}"`로 바꿨다.

**`evals/swebench/local_resolve.py`** — `apply_patch`가 이미 적용된 패치를 다시
적용하려다 예외를 냈다. 채점을 `one_arm.py`와 별도 스크립트로 둔 이유가 "채점
버그를 provider 호출 값 없이 다시 돌리기 위해서"인데 정작 다시 돌릴 수가 없었다.
`git apply --reverse --check`가 통과하면 건너뛴다.

두 수정 후 15개를 다시 채점했고 15행 전부 첫 실행과 같은 값이 나왔다. 위 표의
숫자는 서로 독립인 두 채점 실행이 낸 같은 값이다.

**`evals/swebench/calibrate_one.py`** (신규) — gold 보정을 만드는 드라이버가 없었다.
기존 보정 파일 하나는 임시로 만들어진 것이었다. 이미 있는 보정은 덮어쓰기를
거부한다: 팔을 채점한 뒤에 다시 쓰는 보정은 천장을 결과에 맞추는 짓이다.

## 선택 변경, 숨기지 않고 기록

처음 고른 다섯 중 `django__django-15629`와 `django__django-16263`은
`one_arm.py`가 보는 200개 풀 밖이었다(내가 300에서 골랐다). **규칙은 그대로 두고**
— django, gold 파일 2개 이상, gold 파일 수 내림차순, 11532 제외 — 200 풀에 다시
적용해 `11734`와 `13195`로 바꿨다. `evals/swebench/SELECTION_three_arm.json`의
`amendment` 필드에 적혀 있다.

## 보정

다섯 인스턴스 모두 gold 패치로 깨끗하게 보정됐다. 환경 때문에 깨지는 테스트는
0개라서 제외된 것이 없다. `django__django-13195`만 FAIL_TO_PASS 5개 중 4개가
여기서 달성 가능하다. 그 4개를 천장으로 썼다.

| instance | django | 실행된 테스트 | F2P 달성가능 | 환경으로 깨진 것 |
|---|---|---|---|---|
| `dj-11138` | 3.0 | 84 | 1/1 | 0 |
| `dj-11400` | 3.0 | 311 | 3/3 | 0 |
| `dj-11734` | 3.0 | 370 | 1/1 | 0 |
| `dj-13121` | 3.2 | 383 | 1/1 | 0 |
| `dj-13195` | 3.2 | 502 | 4/5 | 0 |

## 여기서 확인할 수 없는 것

- 공표된 SWE-bench `resolved`. Docker가 없다(이번 세션 재확인). 채점한 건
  django 자기 테스트 스위트를 gold 천장에 맞춘 것이다. 세 팔 모두 같은 천장을
  받았다.
- codex의 비용. usage에 달러가 없어서 `B_codex`의 $0.00은 0원이 아니라
  보고되지 않음이다.
- dobby의 실제 토큰. 위에 적은 이유로 바닥값만 안다.
- 5 인스턴스, django 하나, 팔당 1회. 여기서 나온 4/5 대 1/5은 이 다섯 개에
  대한 것이지 비율의 추정치가 아니다.
