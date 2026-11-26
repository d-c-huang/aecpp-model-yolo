"""Load screenshot previews as their rows enter the visible part of the library."""
from collections import OrderedDict
from tkinter import ttk

from PIL import Image, ImageTk

import theme


class ThumbnailTree(ttk.Treeview):
    SIZE = (76,43)
    ROW_HEIGHT = 51
    CACHE_SIZE = 128

    def __init__(self,parent,scroll,**kwargs):
        self.scroll = scroll
        self.paths = {}
        self.cache = OrderedDict()
        self.shown = {}
        self.pending = None
        ttk.Style(parent).configure("Thumbnail.Treeview",rowheight=self.ROW_HEIGHT,font=(theme.FONT,9))
        super().__init__(parent,style="Thumbnail.Treeview",yscrollcommand=self.scrolled,**kwargs)
        scroll.configure(command=self.yview)
        for event in ("<Configure>","<<TreeviewOpen>>","<<TreeviewClose>>"):
            self.bind(event,lambda _:self.schedule(),add="+")

    def scrolled(self,*args):
        self.scroll.set(*args)
        self.schedule()

    def schedule(self):
        if self.pending is None:
            self.pending = self.after_idle(self.draw_thumbnails)

    def set_paths(self,paths):
        self.paths = paths
        self.shown.clear()
        self.schedule()

    def draw_thumbnails(self):
        self.pending = None
        visible = {self.identify_row(y) for y in range(0,self.winfo_height(),self.ROW_HEIGHT//2)}
        visible &= self.paths.keys()
        for item in self.shown.keys()-visible:
            if self.exists(item):
                self.item(item,image="")
            del self.shown[item]
        for item in visible-self.shown.keys():
            path = self.paths[item]
            if path not in self.cache:
                tile = Image.new("RGB",self.SIZE,theme.MONITOR)
                try:
                    with Image.open(path) as source:
                        source.draft("RGB",self.SIZE)
                        source.thumbnail(self.SIZE,Image.Resampling.LANCZOS)
                        tile.paste(source,((self.SIZE[0]-source.width)//2,(self.SIZE[1]-source.height)//2))
                except OSError:
                    pass
                self.cache[path] = ImageTk.PhotoImage(tile,master=self)
            self.cache.move_to_end(path)
            photo = self.cache[path]
            self.item(item,image=photo)
            self.shown[item] = photo
        while len(self.cache)>self.CACHE_SIZE:
            self.cache.popitem(last=False)
