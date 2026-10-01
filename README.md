# bom-compliance – pobieranie deklaracji RoHS / REACH z oficjalnych stron producentów

Narzędzie CLI, które czyta BoM (CSV/XLSX) i dla każdej pary **MPN + Manufacturer** pobiera
deklaracje RoHS i REACH (certyfikaty, oświadczenia, deklaracje materiałowe) **wyłącznie
z domen należących do producenta**. Na koniec generuje raport (CSV + XLSX + konsola), listę
„Do uzyskania mailowo” z kontaktami znalezionymi na stronach producentów oraz gotowe
szablony e-maili po angielsku.

## Dlaczego Python

Python ma dojrzałe biblioteki do wszystkiego, czego tu potrzeba: `requests` (HTTP, retry, proxy),
`BeautifulSoup` (parsowanie HTML), `openpyxl` (odczyt i zapis XLSX), `csv` i `urllib.robotparser`
w bibliotece standardowej oraz `pypdf` (odczyt tekstu z PDF do weryfikacji treści dokumentu).
Jest też powszechnie znany w zespołach inżynierskich, więc łatwo dopisać nowe adaptery.

## Instalacja

Wymagany Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # lub: pip install -e .   (instaluje komendę bom-compliance)
```

Testy (bez dostępu do sieci, HTTP jest mockowane):

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Format BoM

Obsługiwane formaty: `.csv` / `.tsv` (separator wykrywany automatycznie: `,` `;` tab `|`;
kodowanie UTF-8, CP1250 albo Latin-1) oraz `.xlsx`. W XLSX nagłówek nie musi być w pierwszym
wierszu, bo narzędzie szuka go w pierwszych 30 wierszach.

Wymagane są kolumny z **MPN** i **producentem**. Opcjonalne: ref. designatory, ilość.
Kolumny są rozpoznawane automatycznie po typowych nazwach, np.:

| kolumna logiczna | przykładowe rozpoznawane nagłówki |
|---|---|
| `mpn` | MPN, Manufacturer Part Number, Mfr. Part #, Mfg Part Number, Part Number |
| `manufacturer` | Manufacturer, Mfr., Mfg, Manufacturer Name, Producent |
| `refdes` | Designator, Reference, RefDes, Ref Des |
| `quantity` | Qty, Quantity, Ilość |

Jeśli nazwy kolumn w pliku są inne, podaj mapowanie w CLI (`--col`) albo w pliku konfiguracyjnym
(`columns:`):

```bash
python -m bom_compliance bom.xlsx --col mpn="Supplier PN" --col manufacturer="Maker Name" --col refdes=Ref
```

Przykład: [`examples/bom_example.csv`](examples/bom_example.csv).

**Walidacja i deduplikacja**
- Puste wiersze są pomijane. Wiersze bez MPN, z MPN typu `N/A`/`DNP`/`TBD` albo bez konkretnego
  producenta (`Generic`, puste pole) trafiają do arkusza „Niepoprawne wiersze” razem z powodem.
- Pozycje są deduplikowane po parze *(znormalizowany producent, MPN bez separatorów)*. Ten sam MPN
  w kilku wierszach lub z wieloma ref. designatorami oznacza jedno pobranie. Ten sam MPN
  u **różnych** producentów to dwie osobne pozycje.

## Uruchomienie

```bash
# sprawdzenie, jak BoM zostanie zinterpretowany (bez zapytań sieciowych)
python -m bom_compliance examples/bom_example.csv --dry-run

# pełne uruchomienie
python -m bom_compliance examples/bom_example.csv -o output -v

# z własną konfiguracją, wolniejszym tempem i tylko dla wybranych producentów
python -m bom_compliance bom.xlsx -c my_config.yaml --delay 5 --only TI --only onsemi
```

Najważniejsze opcje (pełna lista: `--help`):

| opcja | opis |
|---|---|
| `-o/--output` | katalog wyjściowy |
| `-c/--config` | plik YAML nadpisujący [`config/default.yaml`](config/default.yaml) (timeouty, tempo, retry, dane do e-maila) |
| `-m/--manufacturers` | własny rejestr producentów (domyślnie [`config/manufacturers.yaml`](config/manufacturers.yaml)) |
| `--col nazwa=Kolumna` | mapowanie kolumn |
| `--sheet` | arkusz XLSX |
| `--delay`, `--timeout`, `--retries` | tempo zapytań na host, timeout odczytu, liczba ponowień |
| `--no-general` | nie pobieraj ogólnych oświadczeń producentów |
| `--no-contacts` | nie szukaj kontaktów do listy „Do uzyskania mailowo” |
| `--dry-run` | tylko wczytaj i zdeduplikuj BoM |

## Wynik

```
output/
├── documents/
│   ├── onsemi/
│   │   └── NDS331N/
│   │       ├── NDS331N__onsemi__RoHS-REACH__1a2b3c4d.pdf
│   │       └── NDS331N__onsemi__RoHS-REACH__1a2b3c4d.pdf.source.json   ← URL, data pobrania, SHA-256
│   └── Texas_Instruments/
│       └── _shared/                       ← dokumenty zbiorcze / ogólne (pobierane raz, wspólne dla wielu MPN)
│           └── Texas_Instruments__REACH__general__szzq087__9f8e7d6c.pdf
├── report.xlsx            ← Podsumowanie | Pozycje | Pliki | Do uzyskania mailowo | Szablony e-mail | Niepoprawne wiersze
├── report_items.csv       ← tabela per pozycja (separator ";", UTF-8 z BOM, otwiera się wprost w Excelu)
├── report_files.csv       ← wszystkie pobrane pliki: ścieżka, URL źródłowy, URL końcowy, data UTC, SHA-256
├── report_to_request.csv  ← „Do uzyskania mailowo”, pogrupowane per producent
├── email_templates/<Producent>.txt  ← jeden zbiorczy e-mail (EN) na producenta + znaleziony kontakt
└── run.log                ← pełny log (DEBUG)
```

Nazwa pliku: `<MPN>__<Producent>__<rodzaj>__<8 znaków SHA-256>.<ext>`. Pliki zbiorcze mają
w nazwie zakres (`family` / `general`) i oryginalną nazwę ze strony producenta.

### Statusy w raporcie

| status | znaczenie | liczony jako sukces |
|---|---|---|
| POBRANO (dokument dla MPN) | treść dokumentu zawiera dokładny MPN | tak |
| POBRANO – DOKUMENT ZBIORCZY | dokument dla rodziny/serii/obudowy; w treści jest tylko prefiks MPN | tak (`count_family_as_success`), ale pozycja trafia też na listę mailową z prośbą o deklarację dla MPN |
| TYLKO OGÓLNE OŚWIADCZENIE | oświadczenie producenta dla całego portfela, bez MPN | nie (`count_general_as_success`) |
| WYMAGA LOGOWANIA | dokument dostępny tylko po zalogowaniu (np. TI myTI) | nie |
| DOSTĘPNE PRZEZ FORMULARZ | na stronie compliance jest formularz zamówienia dokumentu | nie |
| ZABLOKOWANE (robots.txt) | robots.txt producenta zabrania pobrania | nie |
| NIE ZNALEZIONO / BŁĄD / NIEZNANY PRODUCENT | szczegóły w kolumnie „Powód niepowodzenia” | nie |

Rodzaj dokumentu (RoHS / REACH / MCD) i jego zakres są ustalane **na podstawie treści**
pobranego pliku (tekst z PDF/XML/XLSX). Narzędzie sprawdza, czy w treści występuje MPN, a jeśli
nie, to czy występuje prefiks rodziny. Gdy tekstu nie da się odczytać (np. skan), raport podaje
„MPN potwierdzony w treści: unknown” z adnotacją „do ręcznej weryfikacji”.

## Gwarancja pochodzenia plików

- Każdy producent ma w rejestrze listę **oficjalnych domen**. Każde zapytanie, łącznie
  z **każdym krokiem przekierowania**, jest sprawdzane: przekierowanie do dystrybutora albo
  agregatora jest odrzucane, zanim cokolwiek zostanie pobrane.
- Linki do domen innych niż domeny producenta są ignorowane już na etapie wyszukiwania.
- Producent spoza rejestru dostaje status `NIEZNANY PRODUCENT` i nic nie jest pobierane. Narzędzie
  nie szuka plików w wyszukiwarkach internetowych, bo nie dałoby się wtedy zagwarantować źródła.
- Do każdego pliku zapisywany jest sidecar `.source.json` (URL źródłowy, URL końcowy, data
  pobrania w UTC, SHA-256), a te same dane trafiają do arkusza „Pliki”.
- Raport nie zawiera wymyślonych adresów e-mail. Adres trafia do raportu tylko wtedy, gdy znaleziono
  go na pobranej stronie producenta i należy do jego domeny (podawana jest strona źródłowa). W każdym
  innym przypadku raport pokazuje „DO RĘCZNEJ WERYFIKACJI”.

## Uprzejmość wobec serwisów

- `robots.txt` jest respektowany zgodnie z RFC 9309: brak pliku oznacza brak ograniczeń, a gdy plik
  jest nieosiągalny (5xx, błąd sieci), z danego hosta nic nie jest pobierane. Uwzględniany jest też
  `Crawl-delay`.
- Tempo zapytań to domyślnie co najmniej 2 s między zapytaniami do tego samego hosta, plus losowy jitter.
- Retry z wykładniczym backoffem dla 429/5xx i błędów sieci, z uwzględnieniem nagłówka `Retry-After`.
- Strony i dokumenty są cache'owane w obrębie przebiegu, więc wspólne strony compliance
  i oświadczenia zbiorcze są pobierane tylko raz.
- Narzędzie nie obchodzi logowania, CAPTCHA ani formularzy. Takie przypadki oznacza w raporcie.

## Obsługiwani producenci

| producent (wraz z przejętymi markami) | adapter | metoda |
|---|---|---|
| onsemi (ON Semiconductor, Fairchild) | `onsemi` | stały endpoint eksportu per OPN: Certificate of Compliance RoHS & REACH + Material Composition |
| NXP (Freescale) | `nxp` | `nxp.com/mcds/{PN}.pdf` + strona chemical content + strona części |
| Nexperia | `nexperia` | `nexperia.com/chemical-content/{typ}.html` (numer zamówieniowy `BAS16,215` → typ `BAS16`) |
| STMicroelectronics | `st` | strona CPN w eStore → link do deklaracji materiałowej (`/resource/en/material_declaration/`) |
| Microchip (Atmel, Microsemi, Micrel…) | `microchip` | wzorzec `EU-RoHS-CoC_{MPN}.pdf` + ogólne oświadczenia; PMC Search jako strona do ręcznej weryfikacji |
| Analog Devices (Linear, Maxim) | `adi` | strona Material Declarations z parametrem `part` → PDF-y deklaracji; Material Declaration Search do ręcznej weryfikacji |
| Infineon (IR, Cypress) | `infineon` | strona `infineon.com/part/{nazwa}` → Material Content Data Sheet |
| Texas Instruments (Burr-Brown, National) | `ti` | dane per MPN są dostępne tylko po zalogowaniu (Material Content Search), więc status to WYMAGA LOGOWANIA; pobierane są publiczne oświadczenia RoHS/REACH |
| Vishay, Murata, Würth Elektronik, YAGEO, KEMET, TDK, Bourns, Renesas | `generic` | crawl stron compliance producenta (BFS, limit stron i głębokości) |

Adres strony startowej każdego producenta jest w `config/manufacturers.yaml`. Adresy pochodzą
z oficjalnych domen producentów (stan na 2026-10). Serwisy producentów się zmieniają, dlatego każdy
URL jest weryfikowany przy uruchomieniu (kod HTTP, typ treści, domena). Jeśli producent zmieni
serwis, raport pokaże NIE ZNALEZIONO / BŁĄD oraz stronę do ręcznego sprawdzenia, a nie błędny plik.

## Dodawanie producenta

**1. Tylko konfiguracja (mechanizm generyczny).** W wielu przypadkach to wystarczy. Dopisz wpis
do `config/manufacturers.yaml`:

```yaml
  acme:
    name: ACME Components
    adapter: generic
    domains: [acme-components.com]          # wyłącznie oficjalne domeny
    aliases: [ACME, ACME Components Ltd, Acme Comp.]
    compliance_pages:                       # strony RoHS/REACH producenta (start crawla)
      - https://www.acme-components.com/quality/environmental
    search_urls:                            # opcjonalnie: wyszukiwarka na stronie producenta
      - https://www.acme-components.com/search?q={mpn}
    contact_pages:
      - https://www.acme-components.com/contact
    general_documents:                      # opcjonalnie: ogólne oświadczenia
      - url: https://www.acme-components.com/docs/reach-statement.pdf
        types: [REACH]
        scope: general
```

**2. Dedykowany adapter**, gdy producent ma API albo stały wzorzec URL. Dodaj klasę
w `bom_compliance/adapters/` i zarejestruj ją w `ADAPTERS` w `bom_compliance/adapters/__init__.py`:

```python
from ..models import Candidate, DocType, Scope
from .base import BaseAdapter


class AcmeAdapter(BaseAdapter):
    key = "acme"                      # wartość pola "adapter" w manufacturers.yaml
    generic_fallback = True           # gdy nic nie znaleziono, uruchom crawl stron compliance

    def find_part_documents(self, mpn, ctx, result):
        # a) stały wzorzec URL
        result.add(Candidate(
            url=f"https://www.acme-components.com/compliance/{self.q(mpn)}/rohs.pdf",
            doc_types={DocType.ROHS}, scope=Scope.PART))
        # b) albo strona produktu, z której zbieramy linki do dokumentów
        page = self.fetch_html(f"https://www.acme-components.com/p/{self.q(mpn)}", ctx, result)
        if page:
            soup, url = page
            self.collect_document_links(soup, url, mpn, ctx, result)
```

Adapter zwraca tylko **kandydatów**. Pobieraniem, weryfikacją domeny, robots.txt, retry,
klasyfikacją treści, zapisem i raportem zajmuje się wspólny kod. Gdy dokument wymaga logowania
lub formularza, dopisz URL do `result.login_required` / `result.form_required`. Strony do ręcznego
sprawdzenia dopisz do `result.manual_urls`.

## Ograniczenia

- Strony renderowane wyłącznie przez JavaScript (np. część wyszukiwarek producentów) nie są
  wykonywane. Wtedy adapter oznacza pozycję i podaje stronę do ręcznego sprawdzenia.
- Część producentów chroni serwis przed botami (403/CAPTCHA). Narzędzie tego nie obchodzi
  i raportuje „Odmowa dostępu (403)”.
- Dokument zbiorczy (rodzina) jest rozpoznawany heurystycznie, po prefiksie MPN w treści.
  Zawsze jest oznaczony w raporcie, żeby można go było zweryfikować.
