pub2pdf — standalone Microsoft Publisher (.pub) to PDF converter
=================================================================

No Microsoft Office, no LibreOffice, no Python required. This folder is
self-contained; copy it anywhere (USB stick, another PC) and run.

EASIEST: just DOUBLE-CLICK pub2pdf.exe to open the app. Add files or a whole
folder, choose where the PDFs go, and click Convert — a progress bar and a
per-file log show how the batch is going.

Command line (for scripts / headless batches):

    pub2pdf.exe <path> [options]

      path                a .pub file or a directory containing .pub files
      -o, --output-dir D  write PDFs into D (default: next to each source file)
      -r, --recurse       when path is a directory, also search subdirectories
      -f, --force         overwrite existing PDFs instead of skipping
      --gui               open the app even when a path is given

Examples:

    pub2pdf.exe                       (opens the app)
    pub2pdf.exe newsletter.pub
    pub2pdf.exe C:\flyers -r -o C:\flyers\pdf

How it works (all offline, all local):

    .pub  --pub2xhtml-->  SVG per page  --svg2pdf-->  PDF pages  --> merged PDF

    bin\pub2xhtml.exe   libmspub 0.1.4 — the open-source reverse-engineered
                        Publisher parser (Document Liberation Project),
                        compiled for Windows with MinGW-w64 GCC
    bin\svg2pdf.exe     resvg-based vector SVG-to-PDF renderer (Rust)
    pub2pdf.exe         orchestrator (PyInstaller-frozen Python)

Fidelity notes:

  - libmspub re-renders the layout; complex Publisher features (linked text
    boxes, WordArt, gradients, master pages) may shift or simplify.
    For pixel-perfect output you still need Publisher itself.
  - Text is rendered with fonts available on this machine at conversion
    time; missing fonts fall back to system defaults.
