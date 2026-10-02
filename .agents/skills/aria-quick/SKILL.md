---
name: aria-quick
description: Выполнять маленькие low-risk задачи через quick-режим ARIA без спецификации и церемоний.
---

# ARIA Quick

1. Использовать уже созданный `aria run` и прочитать `context_path`.
2. Если route обнаружил high-risk или задача расширилась, повторить route и перейти в `standard`/`deep`.
3. Определить корневую причину и сделать минимальное по области, но полное по поведению изменение.
4. Выполнить classes из assurance plan (обычно focused). При наличии execution contract использовать `aria verify` и его receipt id; иначе сохранить raw output в `outputs/`. Прочитать фактический output/side effect и записать excerpt + actual result + per-class proof excerpt.
5. Прочитать изменённый результат, проверить итоговый diff и production wiring.
6. Передать один JSON-result по `manifest.closure_contract.result_fields` orchestration-обвязке для закрытия; пользователь не вызывает внутренние команды. Spec, ручные acknowledgments, score и milestone не создавать.
