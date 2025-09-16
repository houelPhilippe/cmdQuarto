#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lanceur de commandes PowerShell avec interface graphique (Windows 11)
- Liste de commandes prédéfinies
- Ajout / édition / suppression de commandes
- Exécution dans PowerShell, sortie console en direct
- Choix du dossier de travail
- Barre de progression indéterminée pendant l'exécution
- Sortie en police fixe (Courier New)
- Menu contextuel clic droit : ouvrir fichier, éditer, supprimer
- Zone commandes redimensionnable avec la souris
- Blocage des opérations tant qu'une commande est en cours
- Coloration syntaxique Markdown/Quarto dans la vue fichier
"""
import json
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "Lanceur de commandes PowerShell"
CONFIG_FILE = Path(__file__).with_name("commands.json")

@dataclass
class CommandItem:
    label: str
    command: str
    file: str | None = None

class CommandModel:
    def __init__(self):
        self.items: list[CommandItem] = []
        self.cwd: str | None = None

    def load(self):
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.cwd = data.get("cwd")
                self.items = [CommandItem(**it) for it in data.get("items", [])]
            except Exception as e:
                messagebox.showwarning(APP_TITLE, f"Impossible de lire {CONFIG_FILE}: {e}\nUn nouveau fichier sera créé.")
                self._write_default()
        else:
            self._write_default()

    def save(self):
        payload = {
            "cwd": self.cwd,
            "items": [asdict(it) for it in self.items],
        }
        CONFIG_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_default(self):
        self.items = [
            CommandItem(label="Quarto: render HTML", command="quarto render index.qmd --to html", file="index.qmd"),
            CommandItem(label="Quarto: render PDF", command="quarto render index.qmd --to pdf", file="index.qmd"),
            CommandItem(label="Lister fichiers", command="Get-ChildItem"),
        ]
        self.cwd = str(Path.cwd())
        self.save()

class PSRunner:
    def __init__(self, output_callback, on_exit):
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.queue = queue.Queue()
        self.output_callback = output_callback
        self.on_exit = on_exit
        self._stop_reader = threading.Event()

    def run(self, command: str, cwd: str | None):
        if self.is_running:
            raise RuntimeError("Un processus est déjà en cours.")
        ps_cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
        try:
            self.proc = subprocess.Popen(
                ps_cmd,
                cwd=cwd or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform.startswith("win") else 0,
            )
        except FileNotFoundError:
            messagebox.showerror(APP_TITLE, "PowerShell introuvable. Assurez-vous d'être sous Windows avec PowerShell dans le PATH.")
            self.proc = None
            return
        self._stop_reader.clear()
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()
        self._pump_output()

    @property
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _reader(self):
        assert self.proc is not None
        for line in self.proc.stdout:
            if self._stop_reader.is_set():
                break
            self.queue.put(line)
        self.queue.put(None)

    def _pump_output(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if item is None:
                    self.output_callback("\n[Processus terminé]\n")
                    self.on_exit()
                    return
                else:
                    self.output_callback(item)
        except queue.Empty:
            pass
        if self.is_running or not self.queue.empty():
            root.after(50, self._pump_output)

    def stop(self):
        if self.is_running and self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self._stop_reader.set()

class App(ttk.Frame):
    def __init__(self, master, model: CommandModel):
        super().__init__(master)
        self.model = model
        self.runner = PSRunner(self.append_output, self.on_process_exit)
        self.prog_running = False
        self._opened_file_path: Path | None = None
        self._build_ui()
        self._set_output_file(None)
        self._refresh_list()
        self.set_status()

        # === Blocage global des événements de la Listbox pendant l'exécution ===
        self.listbox.bind("<Button>", self._block_events_when_running, add="+")
        self.listbox.bind("<Double-Button-1>", self._block_events_when_running, add="+")
        self.listbox.bind("<Key>", self._block_events_when_running, add="+")

        # Raccourci clavier : Échap pour arrêter si un processus tourne
        self.master.bind("<Escape>", lambda e: self.stop_running() if self.runner.is_running else None)

    def _block_events_when_running(self, event):
        """Empêche toute interaction sur la liste quand une commande est en cours."""
        if self.runner.is_running:
            return "break"

    def _build_ui(self):
        self.pack(fill="both", expand=True)

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=8, pady=6)
        self.run_btn = ttk.Button(toolbar, text="Exécuter", command=self.run_selected)
        self.run_btn.pack(side="left", padx=(0,6))
        self.stop_btn = ttk.Button(toolbar, text="Arrêter", command=self.stop_running)
        self.stop_btn.pack(side="left")
        self.stop_btn.config(state="disabled")

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # --- PanedWindow horizontal pour redimensionner
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        # Zone Commandes
        left = ttk.Frame(paned, width=300)
        ttk.Label(left, text="Commandes").pack(anchor="w")
        self.listbox = tk.Listbox(left, height=12, exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<Double-Button-1>", self._on_list_dblclick)
        self.listbox.bind("<Return>", lambda e: self.run_selected())

        # Menu contextuel clic droit
        self.menu_ctx = tk.Menu(self, tearoff=0)
        self.menu_ctx.add_command(label="Ouvrir le fichier associé", command=self.open_associated_file)
        self.menu_ctx.add_command(label="Éditer la commande", command=self.edit_command)
        self.menu_ctx.add_command(label="Supprimer la commande", command=self.delete_command)
        self.listbox.bind("<Button-3>", self._on_right_click)
        self.listbox.bind("<Button-2>", self._on_right_click)

        # Boutons de gestion
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=6)
        self.btn_add = ttk.Button(btns, text="Ajouter", command=self.add_command)
        self.btn_add.pack(side="left")
        self.btn_edit = ttk.Button(btns, text="Éditer", command=self.edit_command)
        self.btn_edit.pack(side="left", padx=6)
        self.btn_del = ttk.Button(btns, text="Supprimer", command=self.delete_command)
        self.btn_del.pack(side="left")

        # Zone Sortie
        right = ttk.Frame(paned)
        ttk.Label(right, text="Sortie").pack(anchor="w")
        self.output_toolbar = ttk.Frame(right)
        self.output_toolbar.pack(fill="x", pady=(2,4))
        ttk.Label(self.output_toolbar, text="Fichier:").pack(side="left")
        self.output_file_var = tk.StringVar(value="")
        self.output_file_label = ttk.Label(self.output_toolbar, textvariable=self.output_file_var)
        self.output_file_label.pack(side="left", padx=(4,0))
        self.save_file_btn = ttk.Button(
            self.output_toolbar,
            text="Enregistrer",
            command=self.save_opened_output_file,
            state="disabled",
        )
        self.save_file_btn.pack(side="right")
        text_container = ttk.Frame(right)
        text_container.pack(fill="both", expand=True)

        self.line_numbers = tk.Text(
            text_container,
            width=4,
            padx=6,
            takefocus=0,
            borderwidth=0,
            highlightthickness=0,
            font=("Courier New", 10),
            state="disabled",
            wrap="none",
        )
        self.line_numbers.pack(side="left", fill="y")

        self.text = tk.Text(text_container, wrap="word", height=16, font=("Courier New", 10))
        self.text.pack(side="left", fill="both", expand=True)
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.config(yscrollcommand=self._sync_text_scroll)
        self.line_numbers.config(background=self.text.cget("background"), foreground="#777777", cursor="arrow")
        self.line_numbers.bind("<MouseWheel>", self._on_line_numbers_mousewheel)
        self.line_numbers.bind("<Button-4>", self._on_line_numbers_mousewheel)
        self.line_numbers.bind("<Button-5>", self._on_line_numbers_mousewheel)
        self._update_line_numbers()
        self.text.edit_modified(False)

        # --- Styles de coloration Markdown/Quarto
        # Choix de couleurs qui passent en thème clair/sombre
        self.text.tag_config("md_heading", foreground="#C586C0")     # titres
        self.text.tag_config("md_bold", foreground="#D19A66")        # **gras**
        self.text.tag_config("md_italic", foreground="#D19A66")      # *italique*
        self.text.tag_config("md_code_inline", foreground="#4FC1FF") # `inline code`
        self.text.tag_config("md_code_fence", foreground="#4FC1FF")  # ```fence``` lignes
        self.text.tag_config("md_code_block", foreground="#9CDCFE")  # contenu des blocs
        self.text.tag_config("md_link", foreground="#61AFEF")        # [txt](url)
        self.text.tag_config("md_yaml", foreground="#A3BE8C")        # front matter ---
        self.text.tag_config("md_quarto", foreground="#E5C07B")      # :::, ```{...}
        self.text.tag_config("md_hr", foreground="#6A9955")          # --- / ___

        # Ajout des panneaux
        paned.add(left, weight=1)
        paned.add(right, weight=3)

        # Bas d'écran
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=(0,8))

        self.cwd_var = tk.StringVar(value=self.model.cwd or "")
        ttk.Label(bottom, text="Dossier de travail:").pack(side="left")
        self.cwd_entry = ttk.Entry(bottom, textvariable=self.cwd_var, width=60)
        self.cwd_entry.pack(side="left", padx=6)
        ttk.Button(bottom, text="Parcourir…", command=self.choose_cwd).pack(side="left")

        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", padx=8, pady=(0,6))
        self.prog = ttk.Progressbar(prog_frame, mode="indeterminate")
        self.prog.pack(fill="x")
        self.prog.stop()

        self.status = ttk.Label(self, anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0,6))

        menubar = tk.Menu(root)
        root.config(menu=menubar)
        filemenu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Fichier", menu=filemenu)
        filemenu.add_command(label="Enregistrer", command=self.save)
        filemenu.add_command(label="Enregistrer le fichier ouvert", command=self.save_opened_output_file)
        filemenu.add_command(label="Quitter", command=root.destroy)
        helpmenu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Aide", menu=helpmenu)
        helpmenu.add_command(label="À propos", command=lambda: messagebox.showinfo(APP_TITLE, "Lanceur de commandes PowerShell\n© 2025"))

    # --- Menu contextuel
    def _on_right_click(self, event):
        # Empêcher toute ouverture du menu si un processus est en cours
        if self.runner.is_running:
            return
        try:
            index = self.listbox.nearest(event.y)
            if index is not None:
                self.listbox.selection_clear(0, tk.END)
                self.listbox.selection_set(index)
                self.listbox.activate(index)
            self.menu_ctx.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu_ctx.grab_release()

    def open_associated_file(self):
        if self.runner.is_running:  # sécurité
            return
        idx = self._selected_index()
        if idx is None:
            return
        item = self.model.items[idx]
        if not item.file:
            messagebox.showinfo(APP_TITLE, "Aucun fichier associé à cette commande.")
            return
        path = Path(item.file)
        if not path.is_absolute():
            path = Path(self.cwd_var.get() or ".") / path
        if not path.exists():
            messagebox.showerror(APP_TITLE, f"Fichier introuvable: {path}")
            return
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            try:
                content = path.read_text(encoding="latin-1")
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"Impossible de lire le fichier: {e}")
                return

        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, content)
        self.text.see(tk.END)

        # Coloration syntaxique si fichier Markdown/Quarto
        if path.suffix.lower() in {".qmd", ".md", ".markdown"}:
            self._apply_markdown_highlighting(content)
            if path.suffix.lower() == ".qmd":
                self._set_output_file(path)
            else:
                self._set_output_file(None)
        else:
            self._set_output_file(None)

        self.set_status(f"Fichier ouvert: {path}")

    # --- Coloration Markdown/Quarto (simple, basée regex)
    def _apply_markdown_highlighting(self, content: str):
        # Nettoyer tags existants
        for tag in self.text.tag_names():
            if tag.startswith("md_"):
                self.text.tag_remove(tag, "1.0", tk.END)

        def idx(pos: int) -> str:
            return f"1.0+{pos}c"

        # Titres ATX : ^#{1,6} .*
        for m in re.finditer(r"(?m)^(#{1,6})\s.*$", content):
            self.text.tag_add("md_heading", idx(m.start()), idx(m.end()))

        # Règles horizontales: lignes de --- ___ ***
        for m in re.finditer(r"(?m)^(?:-{3,}|_{3,}|\*{3,})\s*$", content):
            self.text.tag_add("md_hr", idx(m.start()), idx(m.end()))

        # YAML front matter au début: --- ... ---
        fm = re.match(r"(?s)^---\n.*?\n---\s*\n", content)
        if fm:
            self.text.tag_add("md_yaml", idx(fm.start()), idx(fm.end()))

        # Fenced code blocks (Markdown + Quarto): ```lang / ```{lang} ... ```
        for m in re.finditer(r"(?s)```{[^}\n]+}\s*\n.*?\n```|```[^\n]*\n.*?\n```", content):
            # Lignes de fence
            # début
            fence_open = re.match(r"```[^\n]*\n", content[m.start():])
            if fence_open:
                self.text.tag_add("md_code_fence", idx(m.start()), idx(m.start() + fence_open.end()))
                # Marquer Quarto {..}
                if "{" in fence_open.group(0):
                    self.text.tag_add("md_quarto", idx(m.start()), idx(m.start() + fence_open.end()))
            # fin
            self.text.tag_add("md_code_fence", idx(m.end()-4), idx(m.end()))
            # contenu
            body_start = m.start() + (fence_open.end() if fence_open else 0)
            body_end = m.end() - 3  # exclude trailing ```
            self.text.tag_add("md_code_block", idx(body_start), idx(body_end))

        # Blocs Quarto ::: ... :::
        for m in re.finditer(r"(?ms)^:::+.*?$.*?^:::+\s*$", content):
            self.text.tag_add("md_quarto", idx(m.start()), idx(m.end()))

        # Inline code: `...` (non greedy), ignorer ``` blocs déjà tagués
        for m in re.finditer(r"(?s)(?<!`)`([^`\n]|``(?!`))*?`", content):
            self.text.tag_add("md_code_inline", idx(m.start()), idx(m.end()))

        # Gras : **texte** ou __texte__
        for m in re.finditer(r"(?s)(\*\*|__)[^\n].*?\1", content):
            self.text.tag_add("md_bold", idx(m.start()), idx(m.end()))

        # Italique : *texte* ou _texte_ (éviter de capturer le gras déjà tagué)
        for m in re.finditer(r"(?s)(?<!\*)\*(?!\*)([^*\n]|\*(?=[^*\n]))*?\*(?<!\*)|(?<!_)_(?!_)([^_\n]|_(?=[^_\n]))*?_(?<!_)", content):
            self.text.tag_add("md_italic", idx(m.start()), idx(m.end()))

        # Liens : [texte](url) basique
        for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", content):
            self.text.tag_add("md_link", idx(m.start()), idx(m.end()))

        # Titres Setext (=== ou --- sous la ligne)
        for m in re.finditer(r"(?ms)^(?P<title>[^\n]+)\n(=+|-+)\s*$", content):
            self.text.tag_add("md_heading", idx(m.start("title")), idx(m.end("title")))

    # --- Méthodes diverses
    def _start_progress(self):
        if not self.prog_running:
            self.prog.start(10)
            self.prog_running = True
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

    def _stop_progress(self):
        if self.prog_running:
            self.prog.stop()
            self.prog_running = False
        self.run_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        for item in self.model.items:
            suffix = "  📄" if item.file else ""
            self.listbox.insert(tk.END, item.label + suffix)
        if self.model.items:
            self.listbox.select_set(0)

    def _sync_text_scroll(self, first, last):
        """Keep the line number panel aligned with the text widget."""
        self.line_numbers.yview_moveto(first)

    def _on_text_modified(self, _event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._update_line_numbers()

    def _on_line_numbers_mousewheel(self, event):
        direction = 0
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            direction = -1
        elif getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            direction = 1
        if direction:
            self.text.yview_scroll(direction, "units")
        return "break"

    def _update_line_numbers(self):
        line_count = int(self.text.index("end-1c").split(".")[0])
        width = max(3, len(str(line_count)))
        numbers = "\n".join(f"{i:>{width}}" for i in range(1, line_count + 1)) or "1"
        self.line_numbers.config(state="normal")
        self.line_numbers.delete("1.0", tk.END)
        self.line_numbers.insert("1.0", numbers + "\n")
        self.line_numbers.config(state="disabled", width=max(4, width + 1))

    def _set_output_file(self, path: Path | str | None):
        if path:
            resolved = Path(path)
            self._opened_file_path = resolved
            self.output_file_var.set(resolved.name)
            self.save_file_btn.config(state="normal")
        else:
            self._opened_file_path = None
            self.output_file_var.set("—")
            self.save_file_btn.config(state="disabled")

    def append_output(self, text):
        self.text.insert(tk.END, text)
        self.text.see(tk.END)

    def set_status(self, msg: str | None = None):
        base = f"CWD: {self.cwd_var.get() or '(non défini)'}"
        running = self.runner.is_running
        if running:
            base += "  |  Exécution en cours…"

        # Activer/désactiver la liste et menus
        state = "disabled" if running else "normal"
        self.listbox.config(state=state)
        self.btn_add.config(state=state)
        self.btn_edit.config(state=state)
        self.btn_del.config(state=state)
        self.menu_ctx.entryconfig("Ouvrir le fichier associé", state=state)
        self.menu_ctx.entryconfig("Éditer la commande", state=state)
        self.menu_ctx.entryconfig("Supprimer la commande", state=state)

        # Assurer l'état des boutons Exécuter / Arrêter
        self.stop_btn.config(state=("normal" if running else "disabled"))
        self.run_btn.config(state=("disabled" if running else "normal"))

        self.status.config(text=(msg or base))

    def run_selected(self):
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo(APP_TITLE, "Sélectionnez une commande dans la liste.")
            return
        cmd = self.model.items[idx].command
        self._set_output_file(None)
        self.text.delete("1.0", tk.END)
        try:
            self.runner.run(cmd, self.cwd_var.get() or None)
            self._start_progress()
            self.set_status()
        except RuntimeError as e:
            messagebox.showwarning(APP_TITLE, str(e))

    def _on_list_dblclick(self, event):
        if self.runner.is_running:  # sécurité
            return
        index = self.listbox.nearest(event.y)
        if index is not None:
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(index)
            self.listbox.activate(index)
            self.run_selected()

    def stop_running(self):
        self.runner.stop()
        self._stop_progress()
        self.set_status("Arrêt demandé…")

    def on_process_exit(self):
        self._stop_progress()
        self.set_status()

    def choose_cwd(self):
        if self.runner.is_running:  # sécurité
            return
        initial = self.cwd_var.get() or str(Path.cwd())
        path = filedialog.askdirectory(initialdir=initial, mustexist=True)
        if path:
            self.cwd_var.set(path)
            self.model.cwd = path
            self.model.save()
            self.set_status()

    def save(self):
        self.model.cwd = self.cwd_var.get() or None
        self.model.save()
        self.set_status("Enregistré ✔")

    def save_opened_output_file(self):
        if not self._opened_file_path:
            messagebox.showinfo(APP_TITLE, "Aucun fichier ouvert à enregistrer.")
            return
        content = self.text.get("1.0", "end-1c")
        try:
            self._opened_file_path.write_text(content, encoding="utf-8")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Impossible d'enregistrer le fichier: {e}")
            return
        self.set_status(f"Fichier enregistré: {self._opened_file_path}")

    def _selected_index(self):
        try:
            sel = self.listbox.curselection()
            if not sel:
                return None
            return int(sel[0])
        except Exception:
            return None

    def add_command(self):
        if self.runner.is_running:  # sécurité
            return
        self._open_edit_dialog("Ajouter une commande")

    def edit_command(self):
        if self.runner.is_running:  # sécurité
            return
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo(APP_TITLE, "Sélectionnez une commande à éditer.")
            return
        self._open_edit_dialog("Éditer la commande", idx)

    def delete_command(self):
        if self.runner.is_running:  # sécurité
            return
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo(APP_TITLE, "Sélectionnez une commande à supprimer.")
            return
        item = self.model.items[idx]
        if messagebox.askyesno(APP_TITLE, f"Supprimer ‘{item.label}’ ?"):
            del self.model.items[idx]
            self.model.save()
            self._refresh_list()

    def _open_edit_dialog(self, title: str, idx: int | None = None):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Libellé:").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        label_var = tk.StringVar(value=self.model.items[idx].label if idx is not None else "")
        label_entry = ttk.Entry(dialog, textvariable=label_var, width=50)
        label_entry.grid(row=0, column=1, padx=6, pady=6)

        ttk.Label(dialog, text="Commande PowerShell:").grid(row=1, column=0, sticky="e", padx=6, pady=6)
        cmd_var = tk.StringVar(value=self.model.items[idx].command if idx is not None else "")
        cmd_entry = ttk.Entry(dialog, textvariable=cmd_var, width=50)
        cmd_entry.grid(row=1, column=1, padx=6, pady=6)

        ttk.Label(dialog, text="Fichier associé (optionnel):").grid(row=2, column=0, sticky="e", padx=6, pady=6)
        file_var = tk.StringVar(value=self.model.items[idx].file if idx is not None else "")
        file_entry = ttk.Entry(dialog, textvariable=file_var, width=40)
        file_entry.grid(row=2, column=1, padx=6, pady=6)
        ttk.Button(dialog, text="Parcourir…", command=lambda: self._browse_file_into(file_var)).grid(row=2, column=2, padx=6, pady=6)

        def on_ok():
            label = label_var.get().strip()
            cmd = cmd_var.get().strip()
            filev = file_var.get().strip() or None
            if not label or not cmd:
                messagebox.showwarning(APP_TITLE, "Veuillez renseigner le libellé et la commande.")
                return
            item = CommandItem(label=label, command=cmd, file=filev)
            if idx is None:
                self.model.items.append(item)
            else:
                self.model.items[idx] = item
            self.model.save()
            self._refresh_list()
            dialog.destroy()

        ttk.Button(dialog, text="OK", command=on_ok).grid(row=3, column=2, sticky="e", padx=6, pady=10)
        ttk.Button(dialog, text="Annuler", command=dialog.destroy).grid(row=3, column=1, sticky="w", padx=6, pady=10)
        dialog.bind("<Return>", lambda e: on_ok())
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        label_entry.focus_set()

    def _browse_file_into(self, var: tk.StringVar):
        initialdir = self.cwd_var.get() or str(Path.cwd())
        path = filedialog.askopenfilename(initialdir=initialdir)
        if path:
            cwdp = Path(self.cwd_var.get() or ".").resolve()
            try:
                rel = Path(path).resolve().relative_to(cwdp)
                var.set(str(rel))
            except Exception:
                var.set(path)

if __name__ == "__main__":
    root = tk.Tk()
    try:
        root.call("source", "azure.tcl")
        root.call("set_theme", "dark")
    except Exception as e:
        print(f"[WARN] Impossible de charger le thème Azure : {e}")

    root.title(APP_TITLE)
    root.geometry("1000x600")
    model = CommandModel()
    model.load()
    app = App(root, model)
    root.mainloop()
