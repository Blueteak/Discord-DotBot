"""Large native controls for a cloud desktop viewed on a phone."""
import sys
import time

from .launcher import Launcher, LauncherError


def main(directory):
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print('Tkinter is missing. Install your Python distribution’s Tk package (python3-tk on Debian/Ubuntu), then launch dotbot gui again.', file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError:
        print('A graphical desktop is required. Launch dotbot gui in the cloud desktop session with Tk installed.', file=sys.stderr)
        return 1
    try:
        controller = Launcher(directory)
    except Exception:
        root.destroy()
        print('Cannot open the private data folder. Use an untracked --data-dir outside the Discord app source tree.', file=sys.stderr)
        return 1
    Window(root, controller, tk, ttk)
    root.mainloop()
    return 0


class Window:
    def __init__(self, root, controller, tk, ttk):
        self.root, self.controller, self.tk, self.ttk = root, controller, tk, ttk
        self.closing = False
        self.connect_after = 0
        self.preferences = False
        self.page = 0
        self.controls = []
        root.title('Discord DotBot')
        root.geometry('1120x960')
        root.minsize(760, 700)
        root.configure(background='#eef2f7')
        # Negative font sizes are pixels: do not inherit tiny remote Tk DPI.
        root.option_add('*Font', ('Helvetica', -32))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background='#eef2f7')
        style.configure('TLabel', background='#eef2f7', foreground='#17243b', font=('Helvetica', -32))
        style.configure('Help.TLabel', font=('Helvetica', -26))
        style.configure('Title.TLabel', font=('Helvetica', -48, 'bold'))
        style.configure('Status.TLabel', font=('Helvetica', -34, 'bold'))
        style.configure('TEntry', font=('Helvetica', -36), padding=12)
        style.configure('TButton', font=('Helvetica', -34, 'bold'), padding=(24, 18))
        style.configure('TRadiobutton', font=('Helvetica', -32), padding=(10, 14), indicatorsize=28, background='#eef2f7')
        style.configure('TCheckbutton', font=('Helvetica', -30), padding=(10, 14), indicatorsize=28, background='#eef2f7')
        style.configure('Primary.TButton', background='#245dcc', foreground='white')
        style.map('Primary.TButton', background=[('disabled', '#7c8ca8'), ('active', '#19499e')])
        settings = controller.settings
        self.owner = tk.StringVar(value=settings['owner_id'])
        self.handle = tk.StringVar(value=settings.get('owner_handle', ''))
        self.secret = tk.StringVar()
        self.scope = tk.StringVar(value=settings['scope'])
        self.guilds = tk.StringVar(value=', '.join(settings['guild_ids']))
        self.channels = tk.StringVar(value=', '.join(settings['channel_ids']))
        self.listen = tk.BooleanVar(value=settings['listen'] == 'mentions')
        self.audience = tk.BooleanVar(value=settings['audience'] == 'owner')
        self.proxy = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value=controller.status)
        header = ttk.Frame(root, padding=(32, 22, 32, 12))
        header.pack(fill='x')
        ttk.Label(header, text='Discord DotBot', style='Title.TLabel').pack(anchor='w')
        self.step = ttk.Label(header, style='Help.TLabel')
        self.step.pack(anchor='w', pady=(8, 0))
        footer = ttk.Frame(root, padding=(32, 12, 32, 24))
        footer.pack(side='bottom', fill='x')
        ttk.Label(footer, textvariable=self.status, style='Status.TLabel', wraplength=1020).pack(anchor='w')
        ttk.Label(footer, text='Relay only. Ask Dot to keep one active watcher in this session.\nThis app cannot wake a dormant Dot.', style='Help.TLabel', wraplength=1020).pack(anchor='w', pady=(8, 14))
        self.action = ttk.Button(footer, text='Next: choose channels', style='Primary.TButton', command=self.next_or_connect)
        self.action.pack(fill='x')
        self.stop_button = ttk.Button(footer, text='Stop relay', command=self.stop, state='disabled')

        outer = ttk.Frame(root)
        outer.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(outer, background='#eef2f7', highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient='vertical', command=self.canvas.yview)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.body = ttk.Frame(self.canvas, padding=(32, 8, 32, 20))
        self.body_id = self.canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self.resize)
        root.bind('<MouseWheel>', lambda event: self.canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units'))
        root.bind('<Button-4>', lambda event: self.canvas.yview_scroll(-1, 'units'))
        root.bind('<Button-5>', lambda event: self.canvas.yview_scroll(1, 'units'))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.render()
        root.after(250, self.tick)

    def resize(self, event):
        self.canvas.itemconfigure(self.body_id, width=event.width)
        for widget in self.body.winfo_children():
            if isinstance(widget, self.ttk.Label):
                widget.configure(wraplength=max(600, event.width - 80))

    def label(self, text, help=False):
        widget = self.ttk.Label(self.body, text=text, style='Help.TLabel' if help else 'TLabel', wraplength=1000)
        widget.pack(anchor='w', fill='x', pady=(6, 10))
        return widget

    def entry(self, title, variable, masked=False):
        self.label(title)
        widget = self.ttk.Entry(self.body, textvariable=variable, font=('Helvetica', -36), show='•' if masked else '')
        widget.pack(fill='x', pady=(0, 20))
        self.controls.append(widget)
        return widget

    def check(self, text, variable):
        widget = self.ttk.Checkbutton(self.body, text=text, variable=variable)
        widget.pack(anchor='w', fill='x', pady=3)
        self.controls.append(widget)

    def render(self):
        for widget in self.body.winfo_children():
            widget.destroy()
        self.controls = []
        self.canvas.yview_moveto(0)
        if self.page == 0:
            self.step.configure(text='1 of 2 · Your account · Nothing connects until you click Connect')
            self.entry('Your Discord user ID (required)', self.owner)
            self.label('Use Copy User ID in Discord. A handle does not prove ownership.', help=True)
            self.entry('Discord handle (optional, display only)', self.handle)
            if self.controller.saved_token:
                self.label('A token is saved privately. It is never displayed here.', help=True)
                replace = self.ttk.Button(self.body, text='Replace saved token', command=self.replace_token)
                replace.pack(fill='x', pady=10)
                self.controls.append(replace)
            else:
                self.entry('Bot token (hidden)', self.secret, masked=True)
            self.action.configure(text='Next: choose channels')
        else:
            self.step.configure(text='2 of 2 · Choose channels · Connect saves settings and starts the relay')
            for text, value in (('All channels the bot can access', 'accessible'), ('Only specific server and channel IDs', 'scoped')):
                button = self.ttk.Radiobutton(self.body, text=text, variable=self.scope, value=value, command=self.render)
                button.pack(fill='x')
                self.controls.append(button)
            if self.scope.get() == 'accessible':
                self.label('Includes newly accessible server channels and joined threads automatically. Discord permissions control access. DMs, bots and webhooks are excluded.', help=True)
            else:
                self.entry('Server IDs (comma-separated)', self.guilds)
                self.entry('Channel / thread IDs (comma-separated)', self.channels)
            if self.preferences:
                self.check('Require an @mention', self.listen)
                self.check('Receive messages only from the owner', self.audience)
            else:
                summary = ('Owner only' if self.audience.get() else 'All human participants') + (' · @mention required' if self.listen.get() else ' · No @mention required')
                self.label(summary, help=True)
                options = self.ttk.Button(self.body, text='Message options', command=self.show_preferences)
                options.pack(fill='x', pady=8)
                self.controls.append(options)
            self.check('Use this host’s HTTP proxy environment', self.proxy)
            self.label('Leave proxy off unless this host requires it.\nWithout @mention mode, enable Message Content Intent in Discord.', help=True)
            back = self.ttk.Button(self.body, text='Back: account', command=self.back)
            back.pack(fill='x', pady=10)
            self.controls.append(back)
            self.action.configure(text='Connect / Start relay')
        self.set_busy()

    def replace_token(self):
        self.entry('New bot token (hidden; blank keeps saved token)', self.secret, masked=True)
        # Remove the replacement button to avoid duplicate secret fields.
        for widget in self.body.winfo_children():
            if isinstance(widget, self.ttk.Button):
                widget.destroy()
                self.controls.remove(widget)
        self.canvas.yview_moveto(1)

    def show_preferences(self):
        self.preferences = True
        self.render()

    def back(self):
        self.page = 0
        self.render()

    def values(self):
        return dict(self.controller.settings, owner_id=self.owner.get().strip(), owner_handle=self.handle.get().strip(),
                    scope=self.scope.get(), guild_ids=[v.strip() for v in self.guilds.get().split(',') if v.strip()] if self.scope.get() == 'scoped' else [],
                    channel_ids=[v.strip() for v in self.channels.get().split(',') if v.strip()] if self.scope.get() == 'scoped' else [],
                    listen='mentions' if self.listen.get() else 'channels', audience='owner' if self.audience.get() else 'channel')

    def next_or_connect(self):
        if self.controller.busy or self.closing:
            return
        if self.page == 0:
            from .config import snowflake
            try:
                snowflake(self.owner.get())
            except ValueError:
                self.status.set('Enter your 15–20 digit Discord user ID.')
                return
            self.connect_after = time.monotonic() + 0.6
            self.page = 1
            self.render()
            return
        if time.monotonic() < self.connect_after:
            return
        try:
            self.controller.connect(self.values(), self.secret.get(), self.proxy.get())
        except LauncherError as exc:
            self.status.set(str(exc))
            return
        finally:
            self.secret.set('')
        self.status.set(self.controller.status)
        self.set_busy()

    def set_busy(self):
        disabled = self.controller.busy or self.closing
        for widget in self.controls:
            widget.configure(state='disabled' if disabled else 'normal')
        self.action.configure(state='disabled' if disabled or (self.page == 1 and time.monotonic() < self.connect_after) else 'normal')
        if self.controller.busy:
            self.action.pack_forget()
            self.stop_button.pack(fill='x')
        else:
            self.stop_button.pack_forget()
            self.action.pack(fill='x')
        self.stop_button.configure(state='normal' if self.controller.busy and not self.controller.stopping else 'disabled')

    def stop(self):
        self.controller.stop()
        self.status.set(self.controller.status)
        self.set_busy()

    def close(self):
        self.closing = True
        self.secret.set('')
        self.stop()
        if not self.controller.busy:
            self.root.destroy()

    def tick(self):
        was_busy = self.controller.busy
        status = self.controller.poll()
        if was_busy:
            self.status.set(status)
        self.set_busy()
        if self.closing and not self.controller.busy:
            self.root.destroy()
            return
        self.root.after(250, self.tick)
