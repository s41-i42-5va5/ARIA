---
name: aria-spec
description: Проектировать сложные решения через сокращённый deep/spec ARIA; создавать документ только когда он нужен длинной реализации или команде.
---

# ARIA Spec

## ARIA 1.3

Для единого feature lifecycle спецификация подаётся через `aria specify`, уточнения через
`aria clarify`, технический план через `aria plan`, executable graph через `aria tasks`.
Импорт/экспорт Spec Kit выполняется только публичными `aria contract import-spec-kit` и
`aria contract export-spec-kit`; Feature Contract ARIA остаётся каноническим gate.

1. Запустить `aria spec --project <project> --task "<задача>"` либо использовать deep/design run и открыть `context_path`.
2. Уточнить только вопросы, реально меняющие решение; всё остальное решить профессиональным предположением и явно его назвать.
3. Исследовать текущую архитектуру, связанные ADR, архивные решения по ссылкам и реальные интерфейсы кода. Внешний поиск делать при явном запросе или когда актуальные факты, стандарты/API, safety evidence либо реальные альтернативы меняют решение.
4. Сравнить варианты и выбрать один: контракт, границы, данные, ошибки, миграция, тестирование и production wiring.
5. Создать `outputs/feature-contract.json`: measurable outcome, material requirements,
   acceptance oracles, resolved clarifications, implementation plan и ациклический task
   graph. Каждое requirement обязано присутствовать в согласованных acceptance, plan и tasks.
   До создания design proposal заморозить SHA через публичный `aria implement`.
6. Автоматически вызвать независимого подагента `architecture_attacker` с неизменяемым context SHA. Он не меняет код, документы и Git; подтверждённые находки исправляет дирижёр, после чего attacker фиксирует `pass` или `findings_resolved` в `outputs/roles/architecture_attacker.json`. `security_reviewer` добавлять только если он указан в role contract.
7. Для ответа/простого решения не создавать документ. Material source сохранить как reference row (`id`, URL, title, accessed_at, claims); отдельный RESEARCH делать только если его синтез понадобится будущей spec/реализации. ADR создавать только для долговременного выбора с альтернативами или дорогим откатом — research никогда автоматически не превращать в ADR.
8. Для deep/spec сформировать компактную publication-ready Markdown spec в `outputs/` с
   frontmatter `id`, `task_id`, `revision`, `status: approved`, точными `adrs`, `research`,
   `references` и `feature_contract_sha256`. Здесь `approved` означает, что документ прошёл
   техническую проверку; право публикации всё равно принадлежит пользователю.
9. В первом result поставить `status: proposed`, заполнить честные `research_assessment`, `adr_assessment` и `role_evidence`. Закрыть proposal, вывести пользователю: выбранное решение, альтернативы, риски, attacker verdict, пути кандидатов и SHA. Код не писать, spec не публиковать и утверждение не предполагать.
10. После явной фразы пользователя «утверждаю» (либо столь же однозначной) создать approval, привязанный к точным proposal/spec SHA, и завершить тот же design-run. Если пользователь просит изменения, обновить кандидаты, снова провести attack-review и представить новую proposal; прежнее утверждение к новой SHA неприменимо.
11. Active closure публикует связанный набор spec/research/ADR recoverably; shadow оставляет candidates только в runtime. Реализация всегда начинается отдельным deep/build run по уже опубликованной spec.
12. Не использовать score, технические acknowledgments и формальное закрытие микростадий; пользовательское утверждение архитектуры — смысловой gate, а не церемония.
