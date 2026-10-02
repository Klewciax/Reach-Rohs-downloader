# bom-compliance – pobieranie deklaracji RoHS / REACH z oficjalnych stron producentów

Narzędzie CLI, które czyta BoM (CSV/XLSX/XLS) i dla każdej pary **MPN + Manufacturer** pobiera
deklaracje RoHS i REACH (certyfikaty, oświadczenia, deklaracje materiałowe), status cyklu życia
i deklaracje długości produkcji.

- **Źródło podstawowe** to strona producenta.
- **Źródło zapasowe** to API zaufanych dystrybutorów (Octopart/Nexar, DigiKey, Mouser, TME), używane
  tylko wtedy, gdy u producenta nie ma dokumentu. W raporcie zawsze widać, skąd pochodzi plik.
- Producenci spoza rejestru są wykrywani automatycznie, bez ręcznej konfiguracji.

Na koniec narzędzie generuje raport Excel z osobnymi kartami, listę „Do uzyskania mailowo”
z kontaktami i gotowe szablony e-maili po angielsku.

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
python -m pytest              # wszystkie testy (jednostkowe + smoke offline)
python -m pytest -m smoke     # tylko smoke testy
```

## Smoke testy

Smoke testy szybko sprawdzają, czy narzędzie działa jako całość. Są dwa tryby:

```bash
# OFFLINE: bez sieci, nadaje się do CI
python -m bom_compliance.smoke

# LIVE: prawdziwe zapytania do stron producentów (z zachowaniem limitów tempa)
python -m bom_compliance.smoke --live
python -m bom_compliance.smoke --live --only onsemi --only nxp --lifecycle --longevity

# LIVE jako test pytest
BOM_LIVE_SMOKE=1 python -m pytest -m live -s
```

| tryb | co sprawdza |
|---|---|
| offline | konfiguracja i rejestr wczytują się poprawnie, aliasy nie mają konfliktów, **każdy URL i szablon w `manufacturers.yaml` leży na domenie producenta**, wszystkie adaptery istnieją, przykładowy BoM się parsuje; w pytest dodatkowo: `--help`, `--dry-run`, kody błędów CLI oraz pełny przebieg na przykładowym BoM ze wszystkimi opcjami (sieć zamockowana) |
| live | każdy skonfigurowany URL producenta odpowiada (200; logowanie i robots są zgłaszane jako ostrzeżenie), wzorce stron produktu działają, a adaptery znajdują dokumenty dla znanych części z [`config/smoke_parts.yaml`](config/smoke_parts.yaml) |

Kody wyjścia: `0` OK, `1` błąd konfiguracji albo nieaktualny URL (404/410, przekierowanie poza
domenę producenta), `2` problem z siecią (nic nie udało się sprawdzić). Tryb live warto uruchamiać
okresowo, bo serwisy producentów się zmieniają. W GitHub Actions (`.github/workflows/tests.yml`)
smoke offline działa przy każdym pushu, a live można uruchomić ręcznie (*Run workflow → live*).

## Format BoM

Obsługiwane formaty:
- `.xlsx` / `.xlsm`;
- `.xls` (Excel 97-2003, przez `xlrd`);
- `.csv` / `.tsv`: separator wykrywany automatycznie (`,` `;` tab `|`), kodowanie UTF-8, CP1250 albo Latin-1.

Narzędzie samo bada plik i wypisuje wynik analizy przed przetwarzaniem (`--dry-run` pokazuje
tylko analizę):

```
Analiza arkuszy BoM:
  - 'Strona tytułowa': brak kolumn MPN / producent
  - 'BOM': WYBRANY – zawiera BoM (nagłówek w wierszu 3, 11 wierszy danych, kolumny wg nagłówków:
           mpn='Nr kat.', manufacturer='Producent', refdes='Oznaczenie', quantity='Ilość', zamienniki: 1 par kolumn)
  - 'Historia zmian': brak kolumn MPN / producent
```

### Arkusze (karty Excela)

- **Każdy arkusz jest analizowany**, łącznie z ukrytymi. Puste okładki, strony tytułowe i historie
  zmian są pomijane.
- Domyślnie wybierany jest arkusz z **największą liczbą wierszy BoM** (widoczne mają pierwszeństwo).
- `--sheet all` łączy wszystkie arkusze z BoM, np. osobne karty dla kilku płytek. Wiersze w raporcie
  mają wtedy postać `Arkusz!wiersz`, a ten sam MPN z kilku kart to jedna pozycja.
- `--sheet NAZWA` albo `--sheet 2` (numer od 1) wymusza konkretny arkusz.
- Nagłówek może być w dowolnym z pierwszych 40 wierszy (nad tabelą mogą być tytuł i rewizja).
  Obsługiwane są też nagłówki dwupoziomowe, np. „Manufacturer” nad „Name” / „Part Number”.

### Kolumny

Kolumny są rozpoznawane w trzech krokach:

1. **Po nagłówkach**, również polskich:

   | kolumna logiczna | przykładowe nagłówki |
   |---|---|
   | `mpn` | MPN, Manufacturer Part Number, Mfr. Part #, Mfg PN, Nr kat., Numer katalogowy, Kod producenta, Part Number |
   | `manufacturer` | Manufacturer, Mfr., Mfg, Producent, Wytwórca, Brand |
   | `refdes` | Designator, Reference, RefDes, Oznaczenie, Pozycja na schemacie |
   | `quantity` | Qty, Quantity, Ilość, Szt. |

2. **Weryfikacja zawartością.** Jeśli kolumna wskazana nagłówkiem nie zawiera numerów części (np.
   „Part” z opisami albo numerami wewnętrznymi firmy), a inna kolumna je zawiera, narzędzie przełącza
   się na nią i wypisuje ostrzeżenie. Tak samo dla producenta.
3. **Po zawartości, gdy nagłówków nie da się rozpoznać.** Kolumna producenta to ta, której wartości
   rozpoznaje rejestr producentów. Kolumna MPN to ta, której wartości wyglądają jak numery części
   (z wyłączeniem ref. designatorów i ilości). Takie mapowanie jest zawsze oznaczone
   „zweryfikuj”.

Zawsze można też podać mapowanie jawnie: `--col mpn="Nr kat." --col manufacturer="Producent"`
albo `columns:` w pliku konfiguracyjnym.

**Zamienniki (2. źródło).** Kolumny „Manufacturer 2 / MPN 2”, „Producent 2 / Nr kat. 2”,
„Alt Manufacturer / Alt MPN” są wykrywane automatycznie. Każdy zamiennik to osobna pozycja
oznaczona „Zamiennik = TAK”, z tymi samymi ref. designatorami. Wyłączenie: `--no-alternates`.

### MPN: skrót czy pełny numer

MPN w BoM bywa pełnym numerem zamówieniowym (`LM358DR`, `ATMEGA328P-AU`, `BAS16,215`), skrótem
lub nazwą produktu (`LM358`, `ATMEGA328P`) albo wzorcem rodziny (`CRCW0603xxxxFKEA`, `STM32F103C8T*`).
Narzędzie to bada:

1. **Czyszczenie komórki** (każda zmiana trafia do kolumny „Uwagi do MPN”):
   - usuwany jest prefiks producenta (`TI LM358DR` → `LM358DR`) i dopisany opis
     (`LM358DR (SOIC-8)` → `LM358DR`);
   - z kilku numerów w jednej komórce (nowa linia, `;`) pierwszy jest główny, a reszta to zamienniki;
   - `/` i `,` należą do MPN (`TJA1051T/3`, `BAS16,215`) i nie dzielą numerów;
   - MPN zapisany w Excelu jako liczba (np. Würth `7443556082`) jest oznaczany, bo można było
     stracić zera wiodące.
2. **Podpowiedzi z BoM.** Jeśli w innej kolumnie wiersza (np. w opisie) jest dłuższy numer
   zaczynający się od MPN (`LM358` → w opisie `LM358DR`), zapisuje go jako kandydata na pełny MPN.
3. **Sprawdzenie na stronie producenta** (strona produktu z `product_pages`):
   - **pełny (potwierdzony)**: numer występuje na stronie samodzielnie;
   - **skrócony**: na stronie są tylko dłuższe numery (warianty zamówieniowe) albo MPN jest nazwą
     produktu bazowego z listą wariantów. Narzędzie rozwija skrót do pełnego numeru, gdy kandydat
     z opisu w BoM jest na liście wariantów producenta albo gdy producent ma tylko jeden wariant.
     Każde rozwinięcie jest oznaczone w raporcie „MPN rozwinięty automatycznie … – zweryfikuj”.
     Przy wielu wariantach raport wypisuje je wszystkie i prosi o uzupełnienie pełnego numeru w BoM.
   - **wzorzec rodziny**: wyszukiwanie po stałej części wzorca;
   - **nie znaleziono na stronie producenta**: prawdopodobna literówka w BoM.

   Wyłączenie: `--no-mpn-check`. Bez rozwijania skrótów: `expand_abbreviated_mpn: false`.
4. **Dopasowanie dokumentu do MPN uwzględnia granice numeru.** Dokument dla `LM358DRG4`,
   `ATMEGA328P-AU` czy `LTC3780EG#PBF` **nie** jest uznawany za dokument dla `LM358DR`,
   `ATMEGA328P` czy `LTC3780EG`. Taki dokument dotyczy innego wariantu i jest oznaczany jako
   zbiorczy. Dla skrótów i wzorców dokument jest zawsze najwyżej „zbiorczy”, bo nie potwierdza
   konkretnego wariantu.
5. **Sufiksy opakowania.** Gdy dla pełnego numeru z sufiksem (`#PBF`, `,215`, `/NOPB`, `-TR`, `+`)
   nic nie znaleziono, narzędzie szuka także formy bez sufiksu. Wynik oznacza jako dokument
   zbiorczy, bo np. `LTC3780EG` i `LTC3780EG#PBF` mogą mieć różny status RoHS.

Kolumny raportu dotyczące MPN: „Arkusz”, „Zamiennik (2. źródło)”, „Komórka MPN (oryginał)”,
„Forma MPN”, „MPN użyty do wyszukiwania”, „Numery u producenta zaczynające się od MPN”,
„Uwagi do MPN”. W mailu do producenta skrót jest opisany jako „base part number – please cover
all orderable variants”.

**Walidacja i deduplikacja**
- Puste wiersze są pomijane. Wiersze bez MPN, z MPN typu `N/A`/`DNP`/`TBD` albo bez konkretnego
  producenta (`Generic`, puste pole) trafiają do arkusza „Niepoprawne wiersze” razem z powodem.
- Pozycje są deduplikowane po parze *(znormalizowany producent, MPN bez separatorów)*, także między
  arkuszami. Ten sam MPN u **różnych** producentów to dwie osobne pozycje.

Przykłady: [`examples/bom_example.csv`](examples/bom_example.csv) oraz
[`examples/bom_example_multisheet.xlsx`](examples/bom_example_multisheet.xlsx) (okładka, BoM na
2. karcie, polskie nagłówki, zamienniki, skróty i wzorce MPN; generator:
`python examples/make_example_xlsx.py`).

## Uruchomienie

```bash
# sprawdzenie, jak BoM zostanie zinterpretowany: arkusze, kolumny, MPN (bez zapytań sieciowych)
python -m bom_compliance examples/bom_example_multisheet.xlsx --dry-run

# pełne uruchomienie
python -m bom_compliance examples/bom_example.csv -o output -v

# tylko RoHS/REACH, bez statusu cyklu życia i długości produkcji (szybciej)
python -m bom_compliance examples/bom_example.csv -o output --no-lifecycle --no-longevity

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
| `--sheet` | arkusz Excela: nazwa, numer (od 1) albo `all`; domyślnie arkusz z największą liczbą wierszy BoM |
| `--no-alternates` | pomiń zamienniki (Manufacturer 2 / MPN 2 itp.) |
| `--no-mpn-check` | nie sprawdzaj na stronie producenta, czy MPN jest pełny czy skrócony |
| `--manufacturer-only` | tylko strony producentów, bez zapasowego źródła u dystrybutorów |
| `--no-discovery` | nie wykrywaj automatycznie stron producentów spoza rejestru |
| `--delay`, `--timeout`, `--retries` | tempo zapytań na host, timeout odczytu, liczba ponowień |
| `--no-general` | nie pobieraj ogólnych oświadczeń producentów |
| `--no-contacts` | nie szukaj kontaktów do listy „Do uzyskania mailowo” |
| `--no-lifecycle` | nie sprawdzaj statusu cyklu życia (domyślnie sprawdzany: Active / NRND / Last Time Buy / EOL) |
| `--no-longevity` | nie sprawdzaj długości produkcji (domyślnie sprawdzana: program longevity producenta i „do kiedy”) |
| `--dry-run` | tylko wczytaj i zdeduplikuj BoM |

## Wynik

Najważniejszy jest **`report.xlsx`**, czyli raport końcowy w Excelu z osobnymi kartami:

| karta | zawartość |
|---|---|
| **Podsumowanie** | liczby i procenty (RoHS, REACH, oba, żaden, statusy cyklu życia, longevity, skróty MPN), analiza arkuszy BoM, linki do folderów z plikami |
| **RoHS** | jeden wiersz na komponent: MPN, producent, ref. designatory, **Status (link do pliku)**, zakres dokumentu, plik, data pobrania, źródło (adres strony producenta jako tekst), powód niepowodzenia |
| **REACH** | to samo dla REACH (w tym SVHC) |
| **Status cyklu życia** | ACTIVE / NRND / LAST TIME BUY / EOL; **Status = link do zapisanej kopii strony producenta**, z której odczytano status; dosłowna etykieta i fragment strony |
| **Długość produkcji** | „DEKLARACJA: produkcja do RRRR” / objęty programem / tylko ogólna polityka / brak deklaracji; rok końca, okres w latach, podstawa daty; **Status = link do dokumentu lub kopii listy longevity** |
| Szczegóły pozycji | wszystkie kolumny naraz (analiza MPN, zamienniki, uwagi); statusy RoHS / REACH również jako linki |
| Pliki | lista wszystkich pobranych plików (link) z URL źródłowym, datą i SHA-256 |
| Do uzyskania mailowo, Szablony e-mail | pozycje do uzyskania od producenta, pogrupowane per producent, z gotowym mailem (EN) |
| Niepoprawne wiersze | wiersze BoM pominięte i powód |

Kolumna **„Status”** przy każdym komponencie (MPN + producent) jest linkiem do **pobranego pliku
w folderze `documents`**, a nie do strony internetowej. Linki są względne, więc działają po
skopiowaniu albo spakowaniu całego folderu wyjściowego. Plik `report.xlsx` musi zostać w tym
samym miejscu względem `documents/`. Gdy pliku nie ma (np. dokument wymaga logowania), komórka
zawiera sam status bez linku. Adres strony producenta jest zawsze podany jako tekst w kolumnie
„Źródło”.

Pobrane pliki trafiają do **osobnych folderów według rodzaju**:

```
output/
├── report.xlsx                         ← RAPORT GŁÓWNY
├── documents/
│   ├── RoHS/
│   │   ├── onsemi/NDS331N/NDS331N__onsemi__RoHS-REACH__2ac5ac06.pdf
│   │   │                  NDS331N__onsemi__RoHS-REACH__2ac5ac06.pdf.source.json  ← URL, data pobrania, SHA-256
│   │   └── Texas_Instruments/_ogolne_i_zbiorcze/Texas_Instruments__RoHS__general__szzq088__….pdf
│   ├── REACH/
│   │   ├── onsemi/NDS331N/NDS331N__onsemi__RoHS-REACH__2ac5ac06.pdf   ← wspólny certyfikat RoHS+REACH: w obu folderach
│   │   └── Texas_Instruments/_ogolne_i_zbiorcze/…szzq087….pdf
│   ├── Status_cyklu_zycia/<Producent>/<MPN>/<MPN>__<Producent>__LIFECYCLE__<data>.html   ← kopia strony ze statusem
│   ├── Dlugosc_produkcji/<Producent>/<MPN>/…LONGEVITY….html | <Producent>/_ogolne_i_zbiorcze/…pdf
│   └── Deklaracje_materialowe/<Producent>/<MPN>/…MCD….pdf      ← deklaracje składu bez sekcji RoHS/REACH
├── email_templates/<Producent>.txt     ← zbiorczy e-mail (EN) na producenta + znaleziony kontakt
├── report_items.csv, report_files.csv, report_to_request.csv, report_lifecycle.csv   ← te same dane w CSV
└── run.log                             ← pełny log (DEBUG)
```

- Dokument dla konkretnego MPN trafia do `<rodzaj>/<Producent>/<MPN>/`.
- Dokumenty zbiorcze (rodzina) i ogólne oświadczenia trafiają do
  `<rodzaj>/<Producent>/_ogolne_i_zbiorcze/`. Są pobierane raz i podlinkowane przy każdym
  komponencie, którego dotyczą.
- Nazwa pliku ma postać `<MPN>__<Producent>__<rodzaj>__<8 znaków SHA-256>.<ext>`.

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

## Status cyklu życia (domyślnie włączony; `--no-lifecycle` wyłącza)

Dla każdej pozycji narzędzie otwiera stronę produktu **na oficjalnej stronie producenta**
(szablony `product_pages` w `manufacturers.yaml`) i odczytuje oznaczenie statusu. Etykiety producentów
są normalizowane do wspólnej skali:

| status | przykładowe etykiety producentów |
|---|---|
| ACTIVE | Active, In Production, Production, Recommended for new designs, active and preferred |
| PREVIEW | Preview, Proposal, Sampling, Pre-production |
| MATURE | Mature |
| NRND | Not Recommended for New Designs, NRND, not for new design |
| LAST_TIME_BUY | Last Time Buy, LIFEBUY, Last Shipments |
| OBSOLETE (EOL) | Obsolete, Discontinued, End of Life, EOL |
| UNKNOWN | brak etykiety albo strona niedostępna → do ręcznej weryfikacji |

- **Zakres statusu.** „Dla MPN” oznacza, że etykieta stoi przy dokładnym numerze zamówieniowym,
  np. w tabeli wariantów (`LM358DR Active` obok `LM358DRG3 Obsolete` daje ACTIVE). „Strona produktu /
  rodziny” oznacza status strony produktu bazowego, np. Microchip `ATMEGA328P` dla `ATMEGA328P-AU`.
  Taki status jest wyraźnie oznaczony w raporcie.
- **Dowody.** Raport zawiera etykietę dosłownie ze strony, URL, datę sprawdzenia, fragment tekstu
  oraz zapisaną kopię strony (`documents/Status_cyklu_zycia/<Producent>/<MPN>/…__LIFECYCLE__<data>.html`),
  do której prowadzi link w kolumnie „Status”.
- **Linki nawigacyjne** typu „Find Obsolete/EOL products” nie są brane za status. Liczy się tylko
  etykieta „Status: …” albo wartość przy MPN.
- **Ostrzeżenia.** Komponenty NRND, LTB i EOL są wypisywane w konsoli. W XLSX mają kolorowy status
  na karcie „Status cyklu życia”.

Strony produktu skonfigurowane są dla: TI (strona sklepu TI dla MPN), ADI, ST (eStore CPN), Microchip,
onsemi, NXP i Infineon. Dla pozostałych producentów dopisz `product_pages` w `manufacturers.yaml`.

## Długość produkcji / longevity (domyślnie włączona; `--no-longevity` wyłącza)

Narzędzie szuka deklaracji producenta, jak długo komponent będzie produkowany. Sprawdza po kolei:

1. **Stronę produktu.** Jeśli zawiera zapis „longevity … N years / until RRRR”, to ten zapis.
2. **Listy programów longevity producenta** (`longevity_pages`), np. ST Product Longevity, NXP Product
   Longevity, Renesas PLP, Infineon Longevity Program. Szuka dokładnego MPN, a jeśli go nie ma,
   prefiksu rodziny, i odczytuje okres (lata), rok początku oraz rok końca. Z tabel bierze wartości
   z kolumn nazwanych wprost („End date”, „Launch”, „Longevity (years)”).
3. **Dokumenty longevity / polityki EOL** (`longevity_documents`), np. Microchip „Product Longevity”
   i „End of Life policy” albo lista ST sensors 10-year longevity. Są pobierane, zapisywane jak inne
   dokumenty (z URL i datą) i przeszukiwane pod kątem MPN.

„Do kiedy (rok)” narzędzie ustala tak:
- data końcowa podana wprost → `explicit`;
- rok początku plus okres → `start+years`;
- w przeciwnym razie pole zostaje puste z adnotacją „do ręcznej weryfikacji”.

Jeśli producent ma tylko ogólną politykę (np. Microchip „client-driven obsolescence”, bez daty
dla MPN), raport pokazuje „NIE” i link do polityki. Pozycja trafia wtedy na listę „Do uzyskania
mailowo”, a szablon e-maila zawiera prośbę o status cyklu życia i deklarację longevity (do kiedy
produkcja, polityka powiadomień EOL).

## Źródła plików i ich wiarygodność

Kolejność źródeł dla każdej pozycji BoM:

1. **Strona producenta** (adapter producenta lub mechanizm generyczny). Pobieranie jest ograniczone
   do oficjalnych domen producenta, także przy każdym przekierowaniu.
2. **Forma MPN bez sufiksu opakowania** (np. `#PBF`, `,215`) na stronie producenta. Wynik jest
   oznaczany jako dokument zbiorczy.
3. **Dystrybutorzy (zapasowe źródło, przez oficjalne API).** Używani tylko, gdy kroki 1–2 nie dały
   dokumentu RoHS **i** REACH dla konkretnego MPN:
   - brana jest tylko oferta, w której **MPN zgadza się dokładnie, a producent to ten sam producent**
     co w BoM. Ten sam MPN innego producenta jest odrzucany;
   - pobierane są dokumenty środowiskowe (RoHS, REACH, SVHC, deklaracje, certyfikaty), a nie karty
     katalogowe. Pliki pochodzą tylko z domen danego dystrybutora albo producenta;
   - jeśli w treści pliku od dystrybutora jest nazwa producenta, to jest to kopia dokumentu
     producenta i liczy się normalnie. Jeśli nazwy producenta nie ma (np. własne oświadczenie
     dystrybutora), plik jest zapisywany jako „ogólne oświadczenie” i **nie** liczy się jako
     deklaracja dla MPN;
   - kolumna **„Źródło pliku”** na kartach RoHS/REACH pokazuje „strona producenta” albo
     „DigiKey (dystrybutor) – kopia dokumentu producenta”. Nazwa pliku od dystrybutora zawiera
     `__z_<Dystrybutor>`;
   - statusy podawane przez dystrybutorów (np. DigiKey „ROHS3 Compliant”, „REACH Unaffected”,
     Mouser „LifecycleStatus”) są w kolumnie „Status wg dystrybutorów (informacyjnie)”. To dane
     pomocnicze, a nie deklaracja.

Wyłączenie dystrybutorów: `--manufacturer-only` albo `distributor_fallback: false`.

### Klucze API dystrybutorów

Strony WWW DigiKey, Mouser, Octopart i TME blokują automaty, a ich regulaminy zabraniają scrapingu,
dlatego narzędzie używa wyłącznie ich oficjalnych API. Klucze są bezpłatne po rejestracji konta
deweloperskiego (limity zapytań zależą od planu). Podaj je w zmiennych środowiskowych:

| źródło | zmienne środowiskowe | co daje |
|---|---|---|
| Nexar / Octopart | `NEXAR_CLIENT_ID`, `NEXAR_CLIENT_SECRET` | dokumenty (w tym zgodności), **strona producenta** (pomaga wykryć nieznanych producentów) |
| DigiKey (API v4) | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` | dokumenty z sekcji „Environmental Information”, status RoHS / REACH, status produktu |
| Mouser (Search API) | `MOUSER_API_KEY` | status RoHS, status cyklu życia, link do karty katalogowej (Mouser API nie udostępnia dokumentów zgodności) |
| TME | `TME_TOKEN`, `TME_APP_SECRET` | dokumenty produktu (w tym deklaracje), producent |

```bash
export DIGIKEY_CLIENT_ID=...  DIGIKEY_CLIENT_SECRET=...
export MOUSER_API_KEY=...
python -m bom_compliance bom.xlsx -o output
```

Bez klucza dane źródło jest pomijane, a konsola wypisuje, których kluczy brakuje. Klucze można też
wpisać do `api_keys:` w pliku konfiguracyjnym, ale **nie commituj takiego pliku**.
`python -m bom_compliance.smoke --live` sprawdza, czy klucze działają.

## Producenci spoza rejestru: wykrywanie automatyczne

Nie trzeba niczego dopisywać ręcznie. Dla producenta, którego nie ma w `manufacturers.yaml`,
narzędzie samo wykrywa jego oficjalną stronę:

1. Zbiera kandydatów na domenę:
   - stronę producenta z Nexar/Octopart;
   - domenę karty katalogowej z API dystrybutorów (z pominięciem domen dystrybutorów i hostingów);
   - domeny utworzone z nazwy, np. „Acme Connectors Ltd” → `acmeconnectors.com`, `acme.com`, … (do
     10 prób).
2. **Weryfikuje każdego kandydata.** Pobiera stronę główną (z poszanowaniem robots.txt) i sprawdza,
   czy tytuł, nazwa witryny albo początek treści zawiera nazwę producenta. Np. „domena na sprzedaż”
   albo strona innej firmy zostaje odrzucona.
3. Na zweryfikowanej domenie działa mechanizm generyczny: przechodzi od strony głównej po stronach
   o środowisku, jakości i zgodności i szuka dokumentów RoHS/REACH.
4. Wynik zapisuje do `config/discovered_manufacturers.yaml` (kopia w katalogu wyników), więc kolejne
   uruchomienia korzystają z niego od razu. W raporcie taki producent ma „Dopasowanie producenta =
   auto” i uwagę „strona wykryta automatycznie: domena (metoda; potwierdzenie) – zweryfikuj”.
5. Jeśli domeny nie da się zweryfikować, a są klucze API, narzędzie sprawdza dystrybutorów (para
   MPN + nazwa producenta z BoM). Bez tego pozycja dostaje status „NIEZNANY PRODUCENT” i trafia na
   listę mailową.

Wyłączenie: `--no-discovery`. Automatycznie wykryty producent nie ma dedykowanego adaptera, więc
skuteczność zależy od tego, czy jego strona ma statyczne linki do dokumentów.

## Pozostałe gwarancje

- Do każdego pliku zapisywany jest sidecar `.source.json` (URL źródłowy, URL końcowy, źródło:
  producent albo dystrybutor, data pobrania w UTC, SHA-256). Te same dane trafiają do karty „Pliki”.
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

## Dodawanie producenta (opcjonalne)

Nie jest wymagane, bo producenci spoza rejestru są wykrywani automatycznie. Wpis w rejestrze
(albo dedykowany adapter) daje jednak lepsze wyniki: podaje strony compliance, wzorce URL,
strony produktu i listy longevity, więc skrypt nie musi zgadywać.

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
    product_pages:                          # status cyklu życia + analiza MPN: strona produktu ({mpn} {mpn_lower} {base} {base_lower})
      - https://www.acme-components.com/product/{base}
    longevity_pages:                        # długość produkcji: lista programu longevity
      - https://www.acme-components.com/longevity
    longevity_documents:                    # długość produkcji: polityka EOL / longevity (PDF)
      - url: https://www.acme-components.com/docs/eol-policy.pdf
        title: "ACME EOL policy"
        scope: general
```

Po dodaniu producenta uruchom `python -m bom_compliance.smoke`, a jeśli masz dostęp do sieci,
także `--live --only acme`. Smoke test od razu pokaże URL spoza domeny producenta albo nieaktualny
adres. Dla trybu live dopisz znaną część producenta do `config/smoke_parts.yaml`.

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
- Excel: komórki z formułami są czytane jako ostatnio zapisana wartość. Plik wygenerowany przez
  program, który nie zapisuje wyników formuł, może mieć puste komórki. Wtedy otwórz go i zapisz
  ponownie w Excelu.
- Rozwinięcie skrótu MPN opiera się na numerach widocznych na stronie produktu producenta. Strony
  ładujące listę wariantów przez JavaScript tego nie pokażą. MPN pozostaje wtedy bez zmian
  z adnotacją w raporcie.
