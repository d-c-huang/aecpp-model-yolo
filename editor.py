import tkinter as tk
from PIL import Image, ImageTk

import theme


class BoxCanvas(tk.Canvas):
    """Annotation geometry is stored in normalized, original-image coordinates."""
    def __init__(self, parent, category, changed, selection):
        super().__init__(parent,background=theme.MONITOR,width=1,height=1,highlightthickness=0,cursor="crosshair")
        self.category, self.changed, self.selection = category, changed, selection
        self.source, self.photo, self.boxes, self.names = None, None, [], []
        self.selected, self.drag, self.enabled = None, None, True
        self.rect = (0,0,1,1)
        self.prompt = ("开始画面标注","导入一部电影，然后生成截图。")
        self.bind("<Configure>", lambda e: self.render())
        self.bind("<Button-1>", self.press)
        self.bind("<B1-Motion>", self.motion)
        self.bind("<ButtonRelease-1>", self.release)

    def load(self, path, boxes, names, *, prompt=None):
        self.source = None
        if prompt:
            self.prompt = prompt
        if path:
            with Image.open(path) as im:
                self.source = im.convert("RGB")
        self.boxes, self.names = boxes, names
        self.selected = self.drag = None
        self.render()

    def render(self):
        self.delete("all")
        cw,ch = max(1,self.winfo_width()),max(1,self.winfo_height())
        if not self.source:
            self.photo = None
            self.empty_monitor(cw,ch)
            return
        iw,ih = self.source.size
        margin = min(12,(cw-1)//2,(ch-1)//2)
        scale = min((cw-2*margin)/iw,(ch-2*margin)/ih)
        w,h = max(1,round(iw*scale)), max(1,round(ih*scale))
        left,top = (cw-w)/2, (ch-h)/2
        self.rect = left,top,w,h
        self.photo = ImageTk.PhotoImage(self.source.resize((w,h),Image.Resampling.BILINEAR))
        self.create_rectangle(left-1,top-1,left+w,top+h,outline=theme.BORDER)
        self.create_image(left,top,image=self.photo,anchor="nw")
        self.overlay()

    def empty_monitor(self, width, height):
        if width<150 or height<100:
            return
        cx,cy = width/2,height/2
        for x in range(width//2%42,width,42):
            self.create_line(x,0,x,height,fill="#151b23")
        for y in range(height//2%42,height,42):
            self.create_line(0,y,width,y,fill="#151b23")
        self.create_rectangle(cx-160,cy-57,cx+160,cy+63,fill=theme.MONITOR,outline="")
        for dx,dy in ((-1,-1),(1,-1),(-1,1),(1,1)):
            x,y = cx+dx*24,cy-29+dy*15
            self.create_line(x,y-dy*7,x,y,x-dx*9,y,fill=theme.ACCENT,width=2)
        self.create_oval(cx-3,cy-32,cx+3,cy-26,fill=theme.ACCENT,outline="")
        self.create_text(cx,cy+7,text=self.prompt[0],fill=theme.TEXT,font=(theme.FONT,14,"bold"))
        self.create_text(cx,cy+34,text=self.prompt[1],fill=theme.MUTED,font=(theme.FONT,10),width=max(150,width-36))

    def coords_for(self, box):
        l,t,w,h = self.rect
        _,x,y,u,v = box[:5]
        return l+x*w,t+y*h,l+u*w,t+v*h

    def overlay(self):
        self.delete("box")
        for i, box in enumerate(self.boxes):
            a,b,c,d = self.coords_for(box)
            color = theme.ACCENT if self.selected==i else theme.TEAL
            self.create_rectangle(a,b,c,d,outline=color,width=2,tags="box")
            name = box[5]["role"] if len(box)>5 else ""
            label = self.create_text(a+4,b+2,text=name or self.names[box[0]],fill=theme.INK,
                                     font=(theme.FONT,9,"bold"),anchor="nw",tags="box")
            bg = self.create_rectangle(self.bbox(label),fill=color,outline="",tags="box")
            self.tag_lower(bg,label)
            if self.selected == i:
                for x,y in ((a,b),(a,d),(c,b),(c,d)):
                    self.create_rectangle(x-3,y-3,x+3,y+3,fill=color,outline="",tags="box")

    def point(self, event):
        l,t,w,h = self.rect
        return max(0,min(1,(event.x-l)/w)), max(0,min(1,(event.y-t)/h))

    def box_order(self):
        return sorted(range(len(self.boxes)),key=lambda i:(
            (self.boxes[i][3]-self.boxes[i][1])*(self.boxes[i][4]-self.boxes[i][2]),-i))

    def select_at(self,event):
        self.focus_set()
        left,top,width,height = self.rect
        if not (left<=event.x<=left+width and top<=event.y<=top+height):
            return
        x,y = self.point(event)
        for i in self.box_order():
            _,a,b,c,d = self.boxes[i][:5]
            if a<=x<=c and b<=y<=d:
                self.selected = i
                self.selection()
                self.overlay()
                break

    def press(self,event):
        if not self.source or not self.enabled:
            return
        self.focus_set()
        x,y = self.point(event)
        l,t,w,h = self.rect
        if not (l<=event.x<=l+w and t<=event.y<=t+h):
            return
        # Shift-drag starts a new object inside an existing box.
        order = self.box_order()
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
