// Atlas README 功能示意图（非流程图：三个能力模块各自的可视化示意）
// 输出 docs/architecture.pptx（可编辑源文件）→ 外部转 PNG
const pptxgen = require("pptxgenjs");

const pres = new pptxgen();
pres.defineLayout({ name: "BANNER", width: 13.33, height: 5.2 });
pres.layout = "BANNER";
pres.title = "Atlas 功能示意图";

// 配色：石墨底 / 深青主导 / 琥珀点睛（单一定义处）
const BG = "FFFFFF", TEXT = "1E293B", MUTED = "64748B", HAIR = "E2E8F0";
const TEAL = "0F766E", TEAL_MID = "14B8A6", TEAL_LIGHT = "CCFBF1", TEAL_SOFT = "5EEAD4";
const SLATE = "475569", AMBER = "D97706", AMBER_TINT = "FEF3C7", AMBER_DEEP = "B45309";
const F = "Noto Sans CJK SC";

const s = pres.addSlide();
s.background = { color: BG };

// 顶部 tagline
s.addText("Atlas — long-horizon 任务的执行状态，始终住在一张共享图里", {
  x: 0.5, y: 0.22, w: 12.33, h: 0.4, align: "center", fontSize: 13,
  color: MUTED, fontFace: F, margin: 0,
});

// 三列几何
const COLS = [{ x: 0.5 }, { x: 4.83 }, { x: 9.17 }];
const COLW = 3.67;

// 列间 hairline 分隔
[4.5, 8.83].forEach((x) =>
  s.addShape(pres.shapes.LINE, { x, y: 1.05, w: 0, h: 3.1, line: { color: HAIR, width: 1 } })
);

// ---------- 列 1：图状态记忆（节点-边小图） ----------
const node = (x, y, label, fill, color, dash) =>
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x, y, w: 0.8, h: 0.38, rectRadius: 0.08, fill: { color: fill },
    line: dash ? { color: AMBER, width: 1, dashType: "dash" } : { type: "none" },
  });
const nodeText = (x, y, label, color) =>
  s.addText(label, { x, y, w: 0.8, h: 0.38, align: "center", valign: "middle",
    fontSize: 9.5, color, fontFace: F, margin: 0, bold: true });

const edge = (x1, y1, x2, y2, opts = {}) => {
  const dx = x2 - x1, dy = y2 - y1;
  const flipV = dx * dy < 0; // 走向为“右上-左下”时垂直翻转
  const beginX = Math.min(x1, x2), beginY = flipV ? Math.max(y1, y2) : Math.min(y1, y2);
  const p1IsBegin = x1 === beginX && y1 === beginY; // 箭头语义：终点恒为 (x2,y2)
  const arrowOpt = opts.arrow
    ? p1IsBegin ? { endArrowType: "triangle" } : { beginArrowType: "triangle" }
    : {};
  s.addShape(pres.shapes.LINE, {
    x: beginX, y: Math.min(y1, y2), w: Math.abs(dx), h: Math.abs(dy),
    flipV, line: { color: opts.color || "94A3B8", width: opts.w || 1.5,
      dashType: opts.dash || "solid", ...arrowOpt },
  });
};

// 边（先画，被节点覆盖端点）
edge(2.35, 1.14, 1.55, 1.91);                       // 目标→子任务1
edge(2.35, 1.14, 3.15, 1.91);                       // 目标→子任务2
edge(1.55, 1.91, 3.15, 1.91, { arrow: true });      // 子任务1→子任务2（依赖）
edge(1.0, 2.74, 1.55, 1.91);                        // 约束→子任务1
edge(3.4, 2.74, 3.15, 1.91);                        // 决定→子任务2
edge(2.2, 2.74, 3.4, 2.74, { color: AMBER, dash: "dash", arrow: true, w: 1.75 }); // 作废指针
s.addText("作废", { x: 2.55, y: 2.44, w: 0.5, h: 0.24, fontSize: 8.5, color: AMBER_DEEP,
  fontFace: F, margin: 0, align: "center" });

// 节点
node(1.95, 0.95, "", TEAL);      nodeText(1.95, 0.95, "目标", "FFFFFF");
node(1.15, 1.72, "", TEAL_MID);  nodeText(1.15, 1.72, "子任务", "FFFFFF");
node(2.75, 1.72, "", TEAL_MID);  nodeText(2.75, 1.72, "子任务", "FFFFFF");
node(0.6, 2.55, "", SLATE);      nodeText(0.6, 2.55, "约束", "FFFFFF");
node(1.8, 2.55, "", AMBER_TINT, AMBER_DEEP); nodeText(1.8, 2.55, "决定", AMBER_DEEP);
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 3.0, y: 2.55, w: 0.8, h: 0.38, rectRadius: 0.08,
  fill: { color: AMBER }, line: { type: "none" } });
nodeText(3.0, 2.55, "决定", "FFFFFF");

// ---------- 列 2：结转式上下文管理（迷你图 → 四区组装） ----------
// 迷你状态图
[[5.0, 1.5], [5.35, 2.0], [4.95, 2.5]].forEach(([x, y]) => {
  [[5.0, 1.5, 5.35, 2.0], [5.0, 1.5, 4.95, 2.5], [5.35, 2.0, 4.95, 2.5]].forEach(([a, b, c, d]) =>
    edge(a + 0.09, b + 0.09, c + 0.09, d + 0.09, { w: 1 }));
});
[[5.0, 1.5], [5.35, 2.0], [4.95, 2.5]].forEach(([x, y]) =>
  s.addShape(pres.shapes.OVAL, { x, y, w: 0.18, h: 0.18, fill: { color: TEAL_MID }, line: { type: "none" } }));
s.addText("状态图", { x: 4.78, y: 2.78, w: 0.9, h: 0.26, fontSize: 9, color: MUTED, fontFace: F, margin: 0, align: "center" });
// 注入箭头
edge(5.55, 2.3, 6.05, 2.3, { color: TEAL, arrow: true, w: 1.75 });
// 上下文框
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 6.15, y: 1.05, w: 2.25, h: 2.75, rectRadius: 0.1,
  fill: { color: "FFFFFF" }, line: { color: HAIR, width: 1.25 },
  shadow: { type: "outer", color: "94A3B8", blur: 7, offset: 2, angle: 90, opacity: 0.22 } });
s.addText("模型上下文", { x: 6.15, y: 1.12, w: 2.25, h: 0.3, align: "center", fontSize: 9.5,
  color: MUTED, fontFace: F, margin: 0 });
const zone = (y, label, fill, color) => {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 6.32, y, w: 1.92, h: 0.46, rectRadius: 0.06,
    fill: { color: fill }, line: { type: "none" } });
  s.addText(label, { x: 6.32, y, w: 1.92, h: 0.46, align: "center", valign: "middle",
    fontSize: 9.5, color, fontFace: F, margin: 0, bold: true });
};
zone(1.5, "计划与约束", TEAL, "FFFFFF");
zone(2.06, "已完成工作摘要", TEAL_MID, "FFFFFF");
zone(2.62, "近期原始记录", TEAL_LIGHT, "134E4A");
zone(3.18, "踩坑与提示", "F1F5F9", SLATE);
s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.7, y: 0.78, w: 0.7, h: 0.3, rectRadius: 0.12,
  fill: { color: "FFFFFF" }, line: { color: AMBER, width: 1 } });
s.addText("≤ 8k", { x: 7.7, y: 0.78, w: 0.7, h: 0.3, align: "center", valign: "middle",
  fontSize: 9, color: AMBER_DEEP, fontFace: F, margin: 0, bold: true });

// ---------- 列 3：多智能体编排（角色围绕共享图） ----------
// 中心共享图（三角迷你图）
[[11.0, 2.15], [11.62, 1.82], [11.62, 2.52]].forEach(([a, b], i) => {
  [[11.0, 2.15, 11.62, 1.82], [11.0, 2.15, 11.62, 2.52], [11.62, 1.82, 11.62, 2.52]]
    .forEach(([c, d, e, f]) => edge(c + 0.11, d + 0.11, e + 0.11, f + 0.11, { w: 1 }));
});
[[11.0, 2.15], [11.62, 1.82], [11.62, 2.52]].forEach(([x, y]) =>
  s.addShape(pres.shapes.OVAL, { x, y, w: 0.22, h: 0.22, fill: { color: TEAL }, line: { type: "none" } }));
s.addText("共享状态图", { x: 10.72, y: 2.88, w: 1.4, h: 0.26, fontSize: 9, color: MUTED, fontFace: F, margin: 0, align: "center" });
// 角色圆
const role = (cx, cy, label) => {
  s.addShape(pres.shapes.OVAL, { x: cx - 0.33, y: cy - 0.33, w: 0.66, h: 0.66,
    fill: { color: TEXT }, line: { type: "none" },
    shadow: { type: "outer", color: "94A3B8", blur: 5, offset: 1, angle: 90, opacity: 0.2 } });
  s.addText(label, { x: cx - 0.33, y: cy - 0.33, w: 0.66, h: 0.66, align: "center", valign: "middle",
    fontSize: 10, color: "FFFFFF", fontFace: F, margin: 0, bold: true });
};
role(11.05, 1.28, "规划");
role(9.78, 3.05, "执行");
role(12.28, 3.05, "审计");
// 角色 → 图（实线箭头，读写皆经图）
edge(11.05, 1.28, 11.11, 2.15, { color: TEAL, arrow: true, w: 1.5 });
edge(9.78, 3.05, 11.0, 2.37, { color: TEAL, arrow: true, w: 1.5 });
edge(12.28, 3.05, 11.73, 2.63, { color: TEAL, arrow: true, w: 1.5 });

// ---------- 每列标题 + 副语 ----------
const caption = (col, title, sub, note) => {
  s.addText(title, { x: col.x, y: 3.98, w: COLW, h: 0.36, align: "center", fontSize: 17,
    bold: true, color: TEXT, fontFace: F, margin: 0 });
  s.addText(sub, { x: col.x, y: 4.36, w: COLW, h: 0.3, align: "center", fontSize: 11.5,
    color: MUTED, fontFace: F, margin: 0 });
  s.addText(note, { x: col.x, y: 4.72, w: COLW, h: 0.3, align: "center", fontSize: 10,
    color: MUTED, fontFace: F, margin: 0, italic: true });
};
caption(COLS[0], "图状态记忆", "作废不删除 · 决策链可回溯", "节点带时效与写入者，过期信息自动让位");
caption(COLS[1], "结转式上下文管理", "先结构化入图，再按预算组装", "子任务边界结转，压缩不再丢状态");
caption(COLS[2], "多智能体编排", "协调全经共享图，不传对话历史", "规划定路线 · 执行解题 · 审计核验");

pres.writeFile({ fileName: "docs/architecture.pptx" }).then(() => console.log("written docs/architecture.pptx"));
