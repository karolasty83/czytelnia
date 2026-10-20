# Czytelnia

Dodatek do NVDA autorstwa Karola Olejnika do słuchania lokalnych audiobooków.
Aktualna wersja: 1.01. Wymaga NVDA co najmniej 2025.1.

Biblioteka obsługuje import folderów oraz archiwów ZIP, RAR i 7z, zapamiętuje
miejsce słuchania, zakładki, głośność i prędkość. Zawiera wyłącznik czasowy.
Bibliotekę otwiera NVDA+Shift+C. Pełny podręcznik: Czytelnia-podrecznik.html.

## Zawartość repozytorium

- addon/ — kod dodatku, manifest, podręcznik i informacje licencyjne.
- tests/ — testy automatyczne, bez zapisanych wyników ani nagrań.
- build.py — lokalne budowanie i sprawdzanie zawartości paczki.
- tools/7zip-docs/ — informacje dostawcy zewnętrznego narzędzia.
- runtime-files.json — rozmiary i sumy SHA256 plików wykonywalnych używanych lokalnie.

Repozytorium nie zawiera książek, danych biblioteki, ustawień użytkownika,
logów, roboczych raportów ani wcześniejszych paczek dodatku.

## Budowanie na Windows

Wymagany Python 3. Do lokalnego katalogu projektu trzeba dostarczyć:

- ffplay.exe i ffprobe.exe w katalogu głównym;
- 7z.exe i 7z.dll w tools/.

Pliki te nie są śledzone przez Git i nie zostaną pobrane przez klonowanie.
W przygotowanej lokalnej kopii są już obecne do sprawdzenia budowania.
Ich pochodzenie i informacje licencyjne opisuje addon/THIRD-PARTY.txt;
rozmiary i sumy kontrolne są w runtime-files.json. Nie należy zastępować ich
plikami z nieznanego źródła. Przed dystrybucją binariów trzeba osobno przygotować
materiały dostawców opisane w THIRD-PARTY.txt. To repozytorium źródeł nie publikuje binariów.

Uruchom z katalogu repozytorium:

    py -3.13 build.py

Powstanie czytelnia-1.01.nvda-addon. Skrypt sprawdza składnię Pythona, manifest,
listę plików i zgodność sum kontrolnych plików w paczce. Nie instaluje dodatku
w NVDA. Nie usuwa wcześniejszych paczek.

## Testy

    py -3.13 -m unittest discover -s tests

Na systemie bez Windows część testów jest pomijana. Niektóre testy wymagają
wxPython albo lokalnych narzędzi wykonywalnych. Testy nie zastępują odsłuchu
w rzeczywistym NVDA. Wyniki robocze są wykluczone z Git przez .gitignore.

## Licencja i zależności

Kod Pythona: GPL-3.0-or-later, zgodnie z addon/THIRD-PARTY.txt.
Tekst GPLv3: LICENSE oraz addon/COPYING-GPLv3.txt. Informacje o zewnętrznych
narzędziach: addon/THIRD-PARTY.txt i tools/7zip-docs/.
