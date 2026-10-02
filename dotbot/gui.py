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
        self.page = 0
        self.replacing_token = False
        self.controls = []
        root.title('Discord DotBot')
        root.geometry('1180x840')
        root.minsize(980, 780)
        root.configure(background='#eef2f7')
        # Pixel fonts remain readable without inheriting remote desktop Tk DPI.
        root.option_add('*Font', ('Helvetica', -26))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background='#eef2f7')
        style.configure('TLabel', background='#eef2f7', foreground='#17243b', font=('Helvetica', -26))
        style.configure('Help.TLabel', font=('Helvetica', -22))
        style.configure('Title.TLabel', font=('Helvetica', -38, 'bold'))
        style.configure('Status.TLabel', font=('Helvetica', -23, 'bold'))
        style.configure('TEntry', padding=10)
        style.configure('TButton', font=('Helvetica', -28, 'bold'), padding=(18, 14))
        style.configure('TRadiobutton', font=('Helvetica', -28), padding=(8, 12), indicatorsize=26, background='#eef2f7')
        style.configure('TCheckbutton', font=('Helvetica', -26), padding=(8, 12), indicatorsize=26, background='#eef2f7')
        style.configure('Primary.TButton', background='#245dcc', foreground='white')
        style.map('Primary.TButton', background=[('disabled', '#7c8ca8'), ('active', '#19499e')])
        settings = controller.settings
        self.owner = tk.StringVar(value=settings['owner_id'])
        self.secret = tk.StringVar()
        self.scope = tk.StringVar(value=settings['scope'])
        self.guilds = tk.StringVar(value=', '.join(settings['guild_ids']))
        self.channels = tk.StringVar(value=', '.join(settings['channel_ids']))
        self.listen = tk.BooleanVar(value=settings['listen'] == 'mentions')
        self.audience = tk.BooleanVar(value=settings['audience'] == 'owner')
        self.proxy = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value=controller.status)
        header = ttk.Frame(root, padding=(32, 20, 32, 12))
        header.pack(fill='x')
        ttk.Label(header, text='Discord DotBot', style='Title.TLabel').pack(anchor='w')
        self.step = ttk.Label(header, style='Help.TLabel')
        self.step.pack(anchor='w', pady=(6, 0))
        footer = ttk.Frame(root, padding=(32, 8, 32, 20))
        footer.pack(side='bottom', fill='x')
        status_area = ttk.Frame(footer, height=76)
        status_area.pack(fill='x')
        status_area.pack_propagate(False)
        self.status_label = ttk.Label(status_area, textvariable=self.status, style='Status.TLabel', wraplength=1050)
        self.status_label.pack(anchor='w')
        ttk.Label(footer, text='Relay only. Dot needs one active watcher.', style='Help.TLabel').pack(anchor='w', pady=(0, 12))
        navigation = ttk.Frame(footer)
        navigation.pack(fill='x')
        navigation.columnconfigure(1, weight=1)
        self.back_button = ttk.Button(navigation, text='Back', command=self.back)
        self.back_button.grid(row=0, column=0, padx=(0, 16))
        self.action = ttk.Button(navigation, style='Primary.TButton', command=self.next_or_connect)
        self.action.grid(row=0, column=1, sticky='ew')
        self.stop_button = ttk.Button(navigation, text='Stop relay', command=self.stop, state='disabled')
        self.body = ttk.Frame(root, padding=(32, 8, 32, 12))
        self.body.pack(fill='both', expand=True)
        root.bind('<Configure>', self.resize)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.render()
        root.after(250, self.tick)

    def resize(self, event):
        if event.widget is not self.root:
            return
        width = max(800, event.width - 80)
        self.status_label.configure(wraplength=width)
        for widget in self.body.winfo_children():
            if isinstance(widget, self.ttk.Label):
                widget.configure(wraplength=width)

    def label(self, text, help=False):
        widget = self.ttk.Label(self.body, text=text, style='Help.TLabel' if help else 'TLabel', wraplength=1050)
        widget.pack(anchor='w', fill='x', pady=(4, 8))
        return widget

    def entry(self, title, variable, masked=False):
        self.label(title)
        widget = self.ttk.Entry(self.body, textvariable=variable, font=('Helvetica', -30), show='*' if masked else '')
        widget.pack(fill='x', pady=(0, 16))
        self.controls.append(widget)
        return widget

    def check(self, text, variable):
        widget = self.ttk.Checkbutton(self.body, text=text, variable=variable)
        widget.pack(anchor='w', fill='x', pady=4)
        self.controls.append(widget)

    def button(self, text, command):
        widget = self.ttk.Button(self.body, text=text, command=command)
        widget.pack(fill='x', pady=10)
        self.controls.append(widget)

    def render(self):
        for widget in self.body.winfo_children():
            widget.destroy()
        self.controls = []
        if self.page == 0:
            self.step.configure(text='Account')
            self.entry('Discord user ID - 15 to 20 digits', self.owner)
            self.label('Use Copy User ID, not your @handle.', help=True)
            if self.controller.saved_token and not self.replacing_token:
                self.label('Token saved privately.', help=True)
                self.button('Replace token', self.replace_token)
            else:
                self.entry('Bot token (hidden)', self.secret, masked=True)
                if self.controller.saved_token:
                    self.label('Leave blank to keep the saved token.', help=True)
            self.action.configure(text='Next')
        elif self.page == 1:
            self.step.configure(text='Channel access')
            for text, value in (('All accessible channels', 'accessible'), ('Specific channels', 'scoped')):
                widget = self.ttk.Radiobutton(self.body, text=text, variable=self.scope, value=value, command=self.render)
                widget.pack(fill='x')
                self.controls.append(widget)
            self.label('Follows Discord permissions, including new channels.' if self.scope.get() == 'accessible'
                       else 'Enter server and channel IDs on the next page.', help=True)
            self.check('Use this computer’s network proxy', self.proxy)
            self.label('Uses this computer’s network gateway.\nRequired in Dot’s cloud; usually off on your own computer.', help=True).pack_configure(pady=0)
            self.button('Message options', self.show_preferences)
            self.label('Connect saves settings and starts the relay.', help=True)
            self.action.configure(text='Connect' if self.scope.get() == 'accessible' else 'Next')
        elif self.page == 2:
            self.step.configure(text='Specific channels')
            self.entry('Server IDs (comma-separated)', self.guilds)
            self.entry('Channel / thread IDs (comma-separated)', self.channels)
            self.label('Only these destinations will be enabled.', help=True)
            self.action.configure(text='Connect')
        else:
            self.step.configure(text='Message options')
            self.check('Require an @mention', self.listen)
            self.check('Only receive the owner’s messages', self.audience)
            self.label('Without @mention mode, enable Message Content Intent in Discord.', help=True)
            self.action.configure(text='Done')
        self.set_busy()

    def navigate(self, page):
        self.page = page
        self.connect_after = time.monotonic() + 0.6
        self.status.set(self.controller.status)  # Clear previous field errors.
        self.render()

    def replace_token(self):
        self.replacing_token = True
        self.render()

    def show_preferences(self):
        self.navigate(3)

    def back(self):
        self.navigate(0 if self.page == 1 else 1)

    def values(self):
        return dict(self.controller.settings, owner_id=self.owner.get().strip(),
                    scope=self.scope.get(), guild_ids=[v.strip() for v in self.guilds.get().split(',') if v.strip()] if self.scope.get() == 'scoped' else [],
                    channel_ids=[v.strip() for v in self.channels.get().split(',') if v.strip()] if self.scope.get() == 'scoped' else [],
                    listen='mentions' if self.listen.get() else 'channels', audience='owner' if self.audience.get() else 'channel')

    def next_or_connect(self):
        if self.controller.busy or self.closing or time.monotonic() < self.connect_after:
            return
        if self.page == 0:
            from .config import snowflake
            from .launcher import validate_input
            try:
                snowflake(self.owner.get())
            except ValueError:
                self.status.set('Use a 15 to 20 digit user ID, not your @handle.')
                return
            try:
                settings = dict(self.values(), scope='accessible', guild_ids=[], channel_ids=[])
                validate_input(settings, self.secret.get(), self.controller.saved_token)
            except LauncherError as exc:
                self.status.set(str(exc))
                return
            self.navigate(1)
            return
        if self.page == 3:
            self.navigate(1)
            return
        if self.page == 1 and self.scope.get() == 'scoped':
            self.navigate(2)
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
        self.back_button.configure(state='disabled' if disabled or self.page == 0 else 'normal')
        self.action.configure(state='disabled' if disabled or time.monotonic() < self.connect_after else 'normal')
        if self.controller.busy:
            self.action.grid_remove()
            self.stop_button.grid(row=0, column=1, sticky='ew')
        else:
            self.stop_button.grid_remove()
            self.action.grid(row=0, column=1, sticky='ew')
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
