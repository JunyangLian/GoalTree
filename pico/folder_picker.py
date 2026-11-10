"""Native directory chooser; no credentials, project imports or file reads."""
import json
import sys


def main():
    import tkinter as tk
    from tkinter import filedialog
    root=tk.Tk();root.withdraw()
    root.attributes('-topmost',True)
    try:
        path=filedialog.askdirectory(title='选择 GoalTree 工作区',initialdir=sys.argv[1],mustexist=True,parent=root)
        print(json.dumps({'path':path or None},ensure_ascii=False))
    finally:root.destroy()


if __name__=='__main__':main()
