Прочитай $ARIA_DOCS/STATE.yaml. Выведи:

1. Фаза и прогресс: `done/total` задач, процент
2. Текущая задача (current.task) + acceptance criteria (из spec)
3. Блокеры (если есть)
4. Следующие 2 задачи по приоритету (not_started, depends_on = done)
5. Регрессии из списка `regressions_closed` (свёрнуто одной строкой)
6. Протечки контуров из списка `leaks_closed` (если есть секция)

Формат: компактная таблица на русском. Не читай CHANGELOG/ROADMAP (они обновляются автоматически).
