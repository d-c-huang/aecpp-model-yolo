import tkinter as tk
from PIL import Image, ImageTk


class BoxCanvas(tk.Canvas):
    """Annotation geometry is stored in normalized, original-image coordinates."""
    def __init__(self, parent, category, changed, selection):
        super().__init__(parent, background="#20282e", highlightthickness=0, cursor="crosshair")
        self.category, self.changed, self.selection = category, changed, selection
        self.source, self.photo, self.boxes, self.names = None, None, [], []
        self.selected, self.drag, self.enabled = None, None, True
        self.rect = (0,0,1,1)
        self.bind("<Configure>", lambda e: self.render())
        self.bind("<Button-1>", self.press)
        self.bind("<B1-Motion>", self.motion)
        self.bind("<ButtonRelease-1>", self.release)

    def load(self, path, boxes, names):
        self.source = None
        if path:
            with Image.open(path) as im:
                self.source = im.convert("RGB")
        self.boxes, self.names = boxes, names
        self.selected = self.drag = None
        self.render()

    def render(self):
        self.delete("all")
        if not self.source:
            self.create_text(self.winfo_width()/2, self.winfo_height()/2, text="打开工程并抽取电影画面",
                             fill="#bfccd4", font=("Microsoft YaHei UI",14))
            return
        cw,ch = max(1,self.winfo_width()), max(1,self.winfo_height())
        iw,ih = self.source.size
        scale = min(cw/iw, ch/ih)
        w,h = max(1,round(iw*scale)), max(1,round(ih*scale))
        left,top = (cw-w)/2, (ch-h)/2
        self.rect = left,top,w,h
        self.photo = ImageTk.PhotoImage(self.source.resize((w,h),Image.Resampling.BILINEAR))
        self.create_image(left,top,image=self.photo,anchor="nw")
        self.overlay()

    def coords_for(self, box):
        l,t,w,h = self.rect
        _,x,y,u,v = box[:5]
        return l+x*w,t+y*h,l+u*w,t+v*h

    def overlay(self):
        self.delete("box")
        for i, box in enumerate(self.boxes):
            a,b,c,d = self.coords_for(box)
            color = "#ffcd66" if self.selected == i else "#51eed2"
            self.create_rectangle(a,b,c,d,outline=color,width=2,tags="box")
            name = box[5]["role"] if len(box)>5 else ""
            label = self.create_text(a+3,b+2,text=name or self.names[box[0]], fill="#09231f",
                                     anchor="nw",tags="box")
            bg = self.create_rectangle(self.bbox(label),fill=color,outline="",tags="box")
            self.tag_lower(bg,label)
            if self.selected == i:
                for x,y in ((a,b),(a,d),(c,b),(c,d)):
                    self.create_rectangle(x-3,y-3,x+3,y+3,fill=color,outline="",tags="box")

    def point(self, event):
        l,t,w,h = self.rect
        return max(0,min(1,(event.x-l)/w)), max(0,min(1,(event.y-t)/h))

    def press(self,event):
        if not self.source or not self.enabled:
            return
        self.focus_set()
        x,y = self.point(event)
        l,t,w,h = self.rect
        if not (l<=event.x<=l+w and t<=event.y<=t+h):
            return
        # Shift-drag starts a new object inside an existing box.
        order = sorted(range(len(self.boxes)),key=lambda i:(
            (self.boxes[i][3]-self.boxes[i][1])*(self.boxes[i][4]-self.boxes[i][2]),-i))
        if not event.state & 1:
            handles = ([self.selected] if self.selected is not None else []) + order
            for i in handles:
                box = self.boxes[i]
                _,a,b,c,d = box[:5]
                for hx,hy in ((a,b),(a,d),(c,b),(c,d)):
                    if abs(x-hx)*w<=7 and abs(y-hy)*h<=7:
                        self.selected = i
                        self.drag = ("resize",c if hx==a else a,d if hy==b else b,box.copy(),None)
                        break
                if self.drag:
                    break
            if not self.drag:
                for i in order:
                    box = self.boxes[i]
                    _,a,b,c,d = box[:5]
                    if a<=x<=c and b<=y<=d:
                        self.selected = i
                        self.drag = ("move",x,y,box.copy(),None)
                        break
        if not self.drag:
            self.selected = len(self.boxes)
            self.boxes.append([self.category(),x,y,x,y])
            self.drag = ("draw",x,y,None,None)
        self.selection()
        self.overlay()

    def motion(self,event):
        if not self.drag:
            return
        x,y = self.point(event)
        kind,a,b,old,_ = self.drag
        box = self.boxes[self.selected]
        if kind == "move":
            _,p,q,u,v = old[:5]
            dx,dy = max(-p,min(1-u,x-a)),max(-q,min(1-v,y-b))
            box[1:5] = [p+dx,q+dy,u+dx,v+dy]
        else:
            box[1:5] = [min(a,x), min(b,y), max(a,x), max(b,y)]
        self.overlay()

    def release(self,event):
        if not self.drag:
            return
        self.motion(event)
        kind,_,_,old,_ = self.drag
        self.drag = None
        box = self.boxes[self.selected]
        _,_,w,h = self.rect
        if (box[3]-box[1])*w < 2 or (box[4]-box[2])*h < 2:
            if kind == "draw":
                self.boxes.pop()
                self.selected = None
            else:
                self.boxes[self.selected] = old
        elif box != old:
            self.changed()
        self.overlay()
        self.selection()
