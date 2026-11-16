EQ HD Textures
==============

Upscales EverQuest's textures (the .s3d archives) to four times their size with a
local AI model, then installs them into your EverQuest folder.

Please don't share the upscaled textures
  They're made from EverQuest's game files, which are copyrighted by Daybreak
  Game Company. Uploading, sharing or selling them, or any pack, mod or
  archive that contains them, would infringe Daybreak's copyright and may also
  break EverQuest's terms of service. Everyone renders their own, from their
  own copy of the game. Share this app instead.
  This app isn't affiliated with or endorsed by Daybreak Game Company.
  EverQuest is a registered trademark of Daybreak Game Company LLC.

Starting it
  Double-click "EQ HD Textures.exe" in this folder.
  The first time, Windows may say "Windows protected your PC": click
  "More info", then "Run anyway". (The app isn't signed.)
  The first start downloads the upscaling engine (PyTorch, about 3.7 GB), with
  a progress bar. You need an NVIDIA graphics card.

Using it
  1. Click Render. This takes hours; you can Pause, Stop, or close the window
     and continue later. It never touches EverQuest.
  2. Click Install to copy the upscaled textures into EverQuest.
  3. Start EverQuest with "eqgame.exe patchme". The normal launcher puts the
     original textures back; after using it (for example to patch), click
     Render to re-upscale anything patched, then Install.

Where the data goes, and how much space it takes
  A full install needs about 40 GB, because the upscaled textures are kept
  twice:
  - In the data folder, %LOCALAPPDATA%\eq-hd: about 18 GB of upscaled
    textures plus about 7 GB for the engine. Settings has "Open data folder"
    and "Delete rendered data...", and can move it to a bigger drive.
  - In EverQuest, after Install: the same textures replace the originals, so
    the EverQuest folder grows by about 16 GB.
  Why keep both? The EverQuest launcher puts the original textures back every
  time it runs. With the copies kept, getting them back is a few minutes of
  Install instead of hours of rendering again.

Removing it (uninstall and clean up)
  There's no installer, so there's nothing in "Installed apps". To remove
  everything:
  1. Put EverQuest's original textures back: start the normal EverQuest
     launcher once and let it check the files. (Do this even if you delete
     everything else; until then EverQuest keeps using the HD textures.)
  2. Open EQ HD Textures, go to Settings -> "Delete rendered data...", and
     confirm. This removes the rendered textures, the engine and its downloads
     (about 25 GB) from the data folder: %LOCALAPPDATA%\eq-hd, or the folder
     you chose in Settings. (Without the app: close it and delete that folder.)
  3. Close the window and delete this "EQ HD Textures" folder.
  4. Delete the folder %LOCALAPPDATA%\eq-hd (paste that into Explorer's
     address bar). It holds settings.ini, the app's few settings. Nothing is
     kept anywhere else, not in the registry either.

Licence
  EQ HD Textures is free software under the GNU General Public License v2
  (LICENSE.txt). It bundles Python (PSF License), Qt for Python / PySide6
  (LGPL v3) and uv (MIT / Apache 2.0); their licences are in the runtime
  folder. The upscaling model it downloads, 4x-PBRify_UpscalerV4, is CC0.
