# Windows convenience launcher.
#
# The .pyw extension asks Windows to launch Python without opening a console window.
# Keeping this file tiny makes it clear that the actual GUI lives in app.py.

from app import App

# Construct the Tk root window and enter Tkinter's event loop.
# This only displays data already stored in SQLite; it does NOT download prices.
App().mainloop()
