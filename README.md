# energy-law-agent

Agent SDK Mistral: ekspert ds. prawa energetycznego UE i Polski, z narzędziami
weryfikacji źródeł.

## Narzędzia

| Narzędzie | Źródło | Zwraca |
|---|---|---|
| `get_eur_lex_act(celex, language)` | Publications Office (CELLAR, notice API) | tytuł w wybranym języku, daty (dokument, podpis, publikacja OJ, wejście w życie, termin transpozycji), status in-force, ELI, link EUR-Lex |
| `fetch_pl_act(year, position)` | dziennikustaw.gov.pl (ELI) | tytuł wydania Dz.U., linki PDF, URL ELI |
| `fetch_page(url, max_chars)` | dowolna publiczna strona | tytuł + tekst strony (blokada adresów prywatnych) |
| `web_search(query, limit)` | DuckDuckGo (HTML) | tytuły, URL-e, bez klucza API |

## Uruchomienie

```bash
uv sync
echo 'MISTRAL_API_KEY=<klucz>' > .env
uv run --env-file .env agents chat
uv run --env-file .env python main.py   # smoke test: RED III w EUR-Lex
```

## Test narzędzi bez modelu

```bash
uv run python -c "import asyncio; from tools import get_eur_lex_act; print(asyncio.run(get_eur_lex_act('32023L2413', 'pol'))['title'])"
```

## Wdrożenie

`uv run agents deploy` (deployment zarządzany w Studio). Narzędzia nie wymagają
sekretów; `fetch_page` i `web_fetch` przy wdrożeniu warto prowadzić przez egress
proxy (zmienna `MISTRAL_AGENTS_WEBSEARCH_EGRESS_PROXY_URL`), jeśli dodasz gotowe
narzędzia webowe SDK.
