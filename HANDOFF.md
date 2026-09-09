# Handoff после H6+H7

## Что сделано
- `reindex` теперь фоновый (daemon-поток), добавлен `reindex_status`
- Токен-аутентификация: `auth_token` / `ONEC_AUTH_TOKEN`, через `StaticTokenVerifier`
- README, .env.example, config.json.example обновлены
- CODEREVIEW помечен H6,H7 как fixed

## Что не работает / требует внимания
- Эмбеддер на 192.168.31.34:8081 возвращает 503 (прокси, бэкенд не поднят). Индексация падает при первом батче.
- Граф в SQLite построен (25760 узлов), но векторы в Qdrant отсутствуют.

## Следующий шаг после перезагрузки
1. Поднять эмбеддер (llama.cpp/ollama) на .34 за прокси.
2. Запустить индексацию снова: `python -m src.indexer --full` (или инкрементально, если граф уже есть).
3. Проверить `search_config` (семантический поиск).

## Тестирование auth
- Без токена / неверный токен → 401
- Верный токен → 200 (но MCP требует session ID для методов, это нормально)

## Файлы
- Конфиг: config.json (project_data_dir, embedder base_url, auth_token)
- Граф: ./data/graph.sqlite3
- Код: src/server.py, src/config.py

Состояние: репозиторий закоммичен, готов к перезагрузке.