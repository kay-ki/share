# polyqa

Verarbeitet Sidescan-Mosaike ordnerweise zu geglätteten Polygonen und zeigt nach
jedem Schritt vorher/nachher plus drei Prüfzahlen.

Die Oberfläche läuft auf dem Windows-Rechner, gerechnet wird auf dem Linux-Server.
Beide Seiten haben **getrennte Abhängigkeiten**: Windows braucht nur PySide6,
der Server nur die GDAL-Pakete der Distribution. Kein gemeinsames Environment.

    client/   Windows: PySide6
    server/   Debian 12: python3-gdal, python3-shapely, python3-numpy, python3-pil

## Was wo liegt

    client/              Oberfläche (Python-Quellen)
    server/              Verarbeitung, server/vendor/ = Shapely 2.0.7 entpackt,
                         server/python3-polyqa = python3 mit den entpackten Paketen
    pakete/debian12/     alles für den Server, offline
        ohne-installation/  die Server-Pakete entpackt, für server/python3-polyqa
        server/          .deb: python3-gdal, -shapely, -numpy, -pil samt Abhängigkeiten
        gui/             .deb: Qt-Bibliotheken, xauth, python3-venv (nur für Lokal-Modus)
        wheels/          PySide6 für Python 3.11 (nur für Lokal-Modus)
    pakete/windows/      polyqa/ (.exe), polyqa-py/ (Python mitgeliefert), wheels/
    build/               Windows-Build (läuft auf GitHub Actions), Startdateien

## Server einrichten (Debian 12, ohne Internet)

Zwei Wege. Beide brauchen nur das `python3`, das Debian ohnehin hat.

**A: ohne Installation (empfohlen, ändert nichts am System, kein sudo).**
`pakete/debian12/ohne-installation/` sind die Server-Pakete, nur entpackt.
`server/python3-polyqa` startet python3 mit diesen Bibliotheken; alles andere auf
dem Server sieht davon nichts.

    tar -czf - server | ssh benutzer@server "mkdir -p /opt/polyqa && tar -xzf - -C /opt/polyqa"
    tar -C pakete -czf - debian12/ohne-installation | ssh benutzer@server "tar -xzf - -C /opt/polyqa"
    ssh benutzer@server /opt/polyqa/server/python3-polyqa /opt/polyqa/server/polyqa_server.py discover --data /data/mosaike

In „Verbindung“ dann als Python auf dem Server `/opt/polyqa/server/python3-polyqa`
eintragen. `tar` statt `scp`, weil `scp` die Bibliotheksverknüpfungen nicht
mitnimmt. Wieder weg: `rm -rf /opt/polyqa`.

**B: installieren.** Legt GDAL & Co. ins System, kann vorhandene Bibliotheken auf
den Stand der Pakete heben.

    sudo apt install /opt/polyqa/debian12/server/*.deb

Python auf dem Server ist dann `python3`.

Beide Male muss die letzte Zeile JSON mit den gefundenen Ordnern ausgeben. Kein
pip, kein venv, kein GRASS. Debian 12 liefert Shapely 1.8, der Code braucht 2.x:
deshalb liegt Shapely 2.0.7 entpackt in `server/vendor/` und wird von dort
geladen. Getestet offline in einem frischen Debian 12 (GDAL 3.6.2, numpy 1.24,
Python 3.11): A in einem System, das nur python3 hat, B mit apt.

## Windows einrichten

`pakete/windows/` hat drei Wege, der erste, der läuft, gewinnt:

1. `polyqa\polyqa.exe` – nichts zu installieren, Python steckt drin.
2. `polyqa-py\polyqa.bat` – Python 3.12 liegt im Ordner. Startet nichts,
   `polyqa-mit-konsole.bat` nehmen, dort steht der Fehler.
3. Eigenes Python ab 3.10: `pip install --no-index --find-links wheels PySide6_Essentials`,
   dann `python client\main.py`.

Beide Ordner bringen ein `openssh\ssh.exe` mit. Es wird nur benutzt, wenn Windows
selbst kein `ssh` hat, oder wenn unter „Verbindung → ssh-Programm“ der Pfad steht.

## Debian ohne Windows: Lokal-Modus

Bleibt „Server“ in „Verbindung“ leer, rechnet polyqa ohne ssh auf dem Rechner, auf
dem es läuft. So lässt sich die Oberfläche direkt auf dem Server starten und per
`ssh -X` auf Windows anzeigen. Das braucht auf Windows einen X-Server
(z. B. MobaXterm portabel) und auf dem Server:

    sudo apt install /opt/polyqa/debian12/gui/*.deb
    python3 -m venv ~/polyqa-venv
    ~/polyqa-venv/bin/pip install --no-index --find-links /opt/polyqa/debian12/wheels PySide6_Essentials
    scp -r client benutzer@server:/opt/polyqa/      # vom eigenen Rechner aus

Dann von Windows: `ssh -X benutzer@server ~/polyqa-venv/bin/python /opt/polyqa/client/main.py`.
Über die Leitung gehen dabei Bildschirminhalte statt PNGs, das ist spürbar zäher.

Beim ersten Start über „Verbindung" eintragen:

| Feld | Beispiel |
|---|---|
| Server | `benutzer@server` |
| Port | leer für 22 |
| Schlüsseldatei | leer für `~/.ssh/id_*` |
| Python auf dem Server | `python3` |
| Pfad zu polyqa_server.py | `/opt/polyqa/polyqa_server.py` |
| Datenordner auf dem Server | `/data/mosaike` |
| Ausgabeordner auf dem Server | leer für `polyqa_out` im Datenordner |

„Verbindung testen" meldet, wie viele Ordner gefunden wurden. Die Einstellungen
liegen in `%USERPROFILE%\.polyqa.json`.

Die Anmeldung läuft über einen SSH-Schlüssel ohne Passphrase-Abfrage, weil die
Oberfläche nicht nachfragen kann (`BatchMode=yes`). Den Schlüssel also vorher mit
`ssh-keygen` anlegen, mit `ssh-copy-id` auf den Server bringen und einmal von Hand
`ssh benutzer@server` ausführen. Der Hostschlüssel wird beim ersten Mal
automatisch übernommen (`StrictHostKeyChecking=accept-new`), ein **geänderter**
Hostschlüssel führt weiter zum Abbruch.

## Ablauf

Links die Ordner, darunter die sieben Schritte des aktiven Ordners. „Ordner
rechnen" läuft alle offenen Schritte nacheinander durch und bleibt stehen, sobald
eine Prüfung anschlägt; die Ansicht springt dabei auf den jeweils fertigen
Schritt. Danach heißt der Knopf „trotzdem weiterrechnen". „nur diesen Schritt"
rechnet einen Schritt für alle Raster neu, dann werden auch alle Bilder ab diesem
Schritt neu gezeichnet.

Gerechnet wird als eigener Prozess auf dem Server, nicht in der ssh-Sitzung. Die
Oberfläche startet ihn und fragt alle drei Sekunden den Stand ab. Bricht die
Verbindung ab oder wird die Oberfläche geschlossen, rechnet der Server weiter;
beim nächsten Öffnen des Ordners hängt sich die Oberfläche wieder an. Während
des Laufs heißt der Knopf „anhalten“. Nach Anhalten oder Absturz macht „Ordner
rechnen“ beim ersten Raster weiter, das im laufenden Schritt noch fehlt. Je
Ordner läuft höchstens eine Rechnung.

Gefunden werden `.tif`, `.tiff` und `.png` in allen Unterordnern einer Gruppe.
Georeferenz kommt aus der Datei selbst oder den Begleitdateien (`.tfw`, `.pgw`,
`.wld`, `.aux.xml`), das erledigt GDAL. Fehlt das Koordinatensystem (PNG nur mit
World-Datei), greift der Parameter „CRS-Ersatz“; ohne ihn bricht der Downscale mit
Hinweis ab. Paletten-PNGs werden über ihre Farbtabelle in Grau umgerechnet.
Liegen gleichnamige Dateien in Unterordnern, heißen sie intern `unterordner__name`.
Was unter „Ausschließen“ passt, bleibt liegen, voreingestellt `processed/`.

| Schritt | was passiert | Prüfzahlen |
|---|---|---|
| Downscale | gdalwarp, nearest neighbour, am Zielgitter ausgerichtet, nur als VRT, also ohne Kopie | Pixelgröße, kein Upscale, CRS |
| Graustufen | Luminanz aus RGB oder Palette, aus dem verkleinerten Raster | Bänder, woher NoData kommt, verschobene Pixel |
| Maske | gültige Pixel gegen NoData, blockweise | gültiger Anteil, NoData-Wert, Werte mit über 5 % Anteil |
| Polygonisieren | GDALPolygonize, blockweise, ein Feature je Raster | Fläche ÷ Pixelfläche, Fläche außerhalb des Rasters, Mitte in WGS 84 |
| Löcher schließen | Innenringe unter der Mindestfläche fallen weg | kleinstes Loch danach, geschlossene Löcher, Flächenzuwachs |
| Glätten | Douglas-Peucker mit Bisektion im Abweichungsbudget, dann Chaikin | größte Abweichung, Ecken mit 90 Grad, Geometrie gültig |
| Merge und GPKG | ein Layer je Ordner mit Attributen aus dem Ordnernamen | Features, CRS im Ordner, Attribute |

Downscale steht vor Graustufen, weil nearest neighbour nur Pixel auswählt und
Graustufen je Pixel rechnet: Das Ergebnis ist dasselbe, aber es entsteht keine
Graustufen-Kopie in voller Auflösung. Vorhandene Übersichten (`.ovr`) nutzt nur
die Vorschau, nie die Rechnung, weil gemittelte Übersichten NoData in die Ränder
mischen.

NoData kommt aus der Datei, aus dem Alphakanal oder, wenn beides fehlt, aus
„NoData-Ersatz“. Ein RGB-Pixel ist nur dann NoData, wenn alle drei Bänder den
Wert haben. Ergibt ein gültiges Pixel zufällig genau den NoData-Grauwert, etwa
(255, 255, 254) bei NoData 255, wird es um eins verschoben, statt zum Loch zu
werden; wie viele das waren, zeigt „Grau = NoData, verschoben“. Echte
NoData-Flecken mitten im Bild werden zu Löchern, die kleinen schließt der
nächste Schritt.

Glätten sucht je Ring den größten Douglas-Wert, bei dem das Ergebnis nach Chaikin
noch im Abweichungsbudget liegt. Lange gerade Kanten werden vorher unterteilt, sonst
schneidet Chaikin spitze Ecken über das Budget hinaus ab. Passt ein Ring auch so
nicht, wird es mit weniger Chaikin-Durchläufen versucht, erst danach bleibt er
ungeglättet und zählt unter „Geometrie gültig“ als „ungeglättet“. Der Schritt
schlägt an, wenn das Budget gerissen ist, die Geometrie ungültig ist oder nach dem
Glätten noch über 20 % der Ecken bei 90 Grad liegen. Dann Parameter ändern und
„nur diesen Schritt“.

Die wichtigste Zahl ist **Fläche ÷ Pixelfläche**: Sie muss bei 1,00 liegen. Ein
Wert wie 0,38 heißt, dass die Maske invertiert ist oder das falsche Band
polygonisiert wurde. **Ecken mit 90 Grad** ist die Glätte als Zahl: roh liegt der
Anteil nahe 100 %, nach dem Glätten muss er gegen null gehen.

## Wo was liegt, alles auf dem Server

    <datenordner>/polyqa_work/<gruppe>/1_down … 6_smooth      Zwischenstände (1_down nur .vrt)
    <datenordner>/polyqa_work/<gruppe>/metrics/<raster>.json  alle Prüfzahlen
    <datenordner>/polyqa_work/<gruppe>/preview/               die Bilder
    <datenordner>/polyqa_work/<gruppe>/job.json, job.log      laufende Rechnung
    <ausgabeordner>/<gruppe>.gpkg                             Ergebnis

Gerendert wird dort, wo die Daten liegen: Über die Leitung gehen ein PNG von rund
70 KB und etwas JSON, nie ein Mosaik. Auf dem Windows-Rechner landen die Bilder im
temporären Ordner und werden beim Neurechnen eines Schritts verworfen.

`metrics` ist die Wahrheit darüber, was gelaufen ist: Datei löschen heißt Raster
neu rechnen.

## Last und Speicher auf dem Server

Die Rechnung läuft mit `nice 19` und `ionice` niedrigster Stufe, also nur mit
dem, was andere Prozesse übrig lassen, auf einem Kern. „RAM-Grenze in GB“
(voreingestellt 6) begrenzt den Speicher des Prozesses; wird sie erreicht, endet
das Raster mit „Arbeitsspeicher-Grenze erreicht“, statt dass der Kernel einen
anderen Prozess beendet. GDAL bekommt höchstens 256 MB Cache.

Polygonisiert wird mit GDALPolygonize direkt auf der Maske, blockweise. Im
Arbeitsspeicher landen nur die entstehenden Polygone, nicht das Raster. Alle
anderen Schritte lesen und schreiben in Streifen von 256 Zeilen.

## Verbindung

Jeder Aufruf ist eine eigene SSH-Sitzung; der Windows-OpenSSH-Client kann keine
Verbindungen bündeln. Ein Vorschaubild kostet etwa eine halbe Sekunde, die
Standabfrage während des Rechnens ebenso.
