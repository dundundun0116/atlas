// CompassRAG 架构示意图生成器（README 顶部配图）
// 运行：NODE_PATH=$(npm root -g) node docs/assets/build_architecture.js
// 产出：architecture.pptx（可编辑源）+ 经 soffice/pdftoppm 导出 architecture.png
const pptxgen = require("pptxgenjs");

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.33 × 7.5"
pres.author = "CompassRAG";
pres.title = "CompassRAG 架构";

// ---- 调色板（航海仪器：青绿主色 + 琥珀强调）----
const BG = "F6F8FB";
const PANEL = "FFFFFF";
const BORDER = "E2E8F0";
const INK = "0F172A";
const MUTED = "64748B";
const TEAL = "0F766E";
const TEAL_DARK = "115E59";
const TEAL_TINT = "ECFDF5";
const INDIGO = "4F46E5";
const INDIGO_TINT = "EEF2FF";
const AMBER = "D97706";
const AMBER_TINT = "FFF7ED";
const GREY_BAR = "CBD5E1";
const TRACK = "EEF2F7";
const FONT = "PingFang SC";

const shadow = () => ({ type: "outer", color: "0F172A", blur: 7, offset: 2, angle: 90, opacity: 0.10 });
const softShadow = () => ({ type: "outer", color: "0F172A", blur: 4, offset: 1, angle: 90, opacity: 0.06 });

const slide = pres.addSlide();
slide.background = { color: BG };

// ---------- 顶部标题 ----------
slide.addText("CompassRAG", { x: 0.55, y: 0.26, w: 5, h: 0.5, fontSize: 28, bold: true, color: INK, fontFace: FONT, margin: 0 });
slide.addText("自适应检索决策的多跳问答 agent · 实体图索引 + 逐轮检索 + 充分度早停",
  { x: 0.55, y: 0.76, w: 9, h: 0.3, fontSize: 13, color: MUTED, fontFace: FONT, margin: 0 });
slide.addText("MuSiQue 子集 · 50 题 · 同题集消融", { x: 9.0, y: 0.3, w: 3.78, h: 0.3, fontSize: 12, color: MUTED, fontFace: FONT, align: "right", margin: 0 });

// ---------- 区 A：离线建索引 ----------
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.55, y: 1.25, w: 3.5, h: 5.55, fill: { color: PANEL }, line: { color: BORDER, width: 1 }, rectRadius: 0.1, shadow: softShadow() });
slide.addText("离线 · 建索引", { x: 0.75, y: 1.4, w: 3.1, h: 0.32, fontSize: 15, bold: true, color: TEAL_DARK, fontFace: FONT, margin: 0 });
slide.addText("LLM 调用只发生在建索引期", { x: 0.75, y: 1.7, w: 3.1, h: 0.26, fontSize: 12, color: MUTED, fontFace: FONT, margin: 0 });

const cardA = (y, title, desc, dot) => {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.75, y, w: 3.1, h: 1.05, fill: { color: "F8FAFC" }, line: { color: BORDER, width: 1 }, rectRadius: 0.07 });
  if (dot) slide.addShape(pres.shapes.OVAL, { x: 0.95, y: y + 0.24, w: 0.11, h: 0.11, fill: { color: dot } });
  slide.addText(title, { x: dot ? 1.14 : 0.95, y: y + 0.16, w: 2.8, h: 0.3, fontSize: 14, bold: true, color: INK, fontFace: FONT, margin: 0 });
  slide.addText(desc, { x: 0.95, y: y + 0.52, w: 2.8, h: 0.42, fontSize: 12, color: MUTED, fontFace: FONT, margin: 0 });
};
cardA(1.95, "分块语料", "4,646 块 · 每题证据 + 自带干扰段落");
cardA(3.14, "LLM 实体抽取", "每块 1 次调用 · 实体 + 关系三元组（关思维链）");
cardA(4.33, "实体关系图", "4.1 万节点 · 三元组边 + 同义边", AMBER);
cardA(5.52, "检索索引", "BM25 稀疏 + BGE-M3 稠密向量");

// A → B 调用箭头
slide.addShape(pres.shapes.LINE, { x: 4.06, y: 2.0, w: 0.22, h: 0, line: { color: MUTED, width: 1.25, endArrowType: "triangle" } });

// ---------- 区 B：在线 agent 决策循环（主角）----------
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 4.3, y: 1.25, w: 6.1, h: 5.55, fill: { color: PANEL }, line: { color: BORDER, width: 1 }, rectRadius: 0.1, shadow: shadow() });
slide.addText("在线 · agent 决策循环", { x: 4.5, y: 1.4, w: 3.4, h: 0.32, fontSize: 15, bold: true, color: TEAL_DARK, fontFace: FONT, margin: 0 });
slide.addText("控制流由 LLM 接管：路由 / 计划 / 预算 / 早停", { x: 4.5, y: 1.7, w: 3.9, h: 0.26, fontSize: 12, color: MUTED, fontFace: FONT, margin: 0 });
slide.addText("低预算 · 自适应 1–3 轮", { x: 6.6, y: 1.44, w: 3.6, h: 0.26, fontSize: 12, bold: true, color: AMBER, fontFace: FONT, align: "right", margin: 0 });

// 入口
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 6.65, y: 2.05, w: 1.4, h: 0.38, fill: { color: TEAL_TINT }, line: { color: TEAL, width: 1 }, rectRadius: 0.19 });
slide.addText("多跳问题", { x: 6.65, y: 2.05, w: 1.4, h: 0.38, fontSize: 12.5, bold: true, color: TEAL_DARK, fontFace: FONT, align: "center", valign: "middle", margin: 0 });
slide.addShape(pres.shapes.LINE, { x: 7.35, y: 2.44, w: 0, h: 0.16, line: { color: MUTED, width: 1.25, endArrowType: "triangle" } });

// 决策层卡
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 4.5, y: 2.62, w: 5.7, h: 1.0, fill: { color: TEAL_TINT }, line: { color: "99F6E4", width: 1 }, rectRadius: 0.08 });
slide.addText("决策层（LLM 控制流）", { x: 4.68, y: 2.72, w: 3.4, h: 0.3, fontSize: 14, bold: true, color: INK, fontFace: FONT, margin: 0 });
const chip = (x, w, text) => {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 3.06, w, h: 0.36, fill: { color: PANEL }, line: { color: "99F6E4", width: 1 }, rectRadius: 0.18 });
  slide.addText(text, { x, y: 3.06, w, h: 0.36, fontSize: 12, color: TEAL_DARK, fontFace: FONT, align: "center", valign: "middle", margin: 0 });
};
chip(4.68, 2.05, "路由：直答 / 单跳 / 多跳");
chip(6.85, 1.15, "检索计划");
chip(8.12, 1.25, "预算 ≤ 3 轮");
slide.addShape(pres.shapes.LINE, { x: 7.35, y: 3.63, w: 0, h: 0.16, line: { color: MUTED, width: 1.25, endArrowType: "triangle" } });

// 循环容器
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 4.5, y: 3.8, w: 5.7, h: 2.3, fill: { color: "F8FAFC" }, line: { color: BORDER, width: 1 }, rectRadius: 0.08 });
slide.addText("检索循环（每轮一查，逐轮累积证据）", { x: 4.68, y: 3.87, w: 4.4, h: 0.26, fontSize: 12, bold: true, color: MUTED, fontFace: FONT, margin: 0 });

// 行 1：子问题 → 三路检索
const pill = (x, w, text, fill, line, color) => {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 4.2, w, h: 0.52, fill: { color: fill }, line: { color: line, width: 1 }, rectRadius: 0.07 });
  slide.addText(text, { x, y: 4.2, w, h: 0.52, fontSize: 12.5, bold: true, color, fontFace: FONT, align: "center", valign: "middle", margin: 0 });
};
pill(4.68, 1.45, "子问题查询", PANEL, BORDER, INK);
slide.addShape(pres.shapes.LINE, { x: 6.15, y: 4.46, w: 0.18, h: 0, line: { color: MUTED, width: 1.25, endArrowType: "triangle" } });
const route = (x, w, text, color, tint) => {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 4.2, w, h: 0.52, fill: { color: tint }, line: { color, width: 1 }, rectRadius: 0.07 });
  slide.addShape(pres.shapes.OVAL, { x: x + 0.11, y: 4.435, w: 0.09, h: 0.09, fill: { color } });
  slide.addText(text, { x: x + 0.24, y: 4.2, w: w - 0.26, h: 0.52, fontSize: 12.5, bold: true, color, fontFace: FONT, valign: "middle", margin: 0 });
};
route(6.35, 1.2, "BM25", TEAL, TEAL_TINT);
route(7.65, 1.2, "稠密向量", INDIGO, INDIGO_TINT);
route(8.95, 1.2, "实体图 PPR", AMBER, AMBER_TINT);
slide.addShape(pres.shapes.LINE, { x: 7.75, y: 4.74, w: 0, h: 0.14, line: { color: MUTED, width: 1.25, endArrowType: "triangle" } });

// 行 2：融合 → 累积 → 判断
const step = (x, w, text) => {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 4.9, w, h: 0.52, fill: { color: PANEL }, line: { color: BORDER, width: 1 }, rectRadius: 0.07 });
  slide.addText(text, { x, y: 4.9, w, h: 0.52, fontSize: 12.5, bold: true, color: INK, fontFace: FONT, align: "center", valign: "middle", margin: 0 });
};
step(4.68, 1.75, "RRF 融合 + 近重合并");
slide.addShape(pres.shapes.LINE, { x: 6.45, y: 5.16, w: 0.16, h: 0, line: { color: MUTED, width: 1.25, endArrowType: "triangle" } });
step(6.63, 1.25, "证据累积");
slide.addShape(pres.shapes.LINE, { x: 7.9, y: 5.16, w: 0.16, h: 0, line: { color: MUTED, width: 1.25, endArrowType: "triangle" } });
step(8.08, 1.75, "充分度判断？");

// 回环（不足）：判断 → 沿容器左侧回到 子问题查询（箭头落到其左缘）
slide.addShape(pres.shapes.LINE, { x: 7.95, y: 5.42, w: 0, h: 0.22, line: { color: AMBER, width: 1.25 } });
slide.addShape(pres.shapes.LINE, { x: 4.6, y: 5.64, w: 3.35, h: 0, line: { color: AMBER, width: 1.25 } });
slide.addShape(pres.shapes.LINE, { x: 4.6, y: 5.64, w: 0, h: -1.18, line: { color: AMBER, width: 1.25 } });
slide.addShape(pres.shapes.LINE, { x: 4.6, y: 4.46, w: 0.08, h: 0, line: { color: AMBER, width: 1.25, endArrowType: "triangle" } });
slide.addText("不足 · 换查询再来一轮", { x: 5.55, y: 5.68, w: 2.5, h: 0.26, fontSize: 12, color: AMBER, fontFace: FONT, margin: 0 });

// 出口（充分）：判断 → 答案 + 证据链
slide.addShape(pres.shapes.LINE, { x: 9.6, y: 5.42, w: 0, h: 0.74, line: { color: TEAL, width: 1.5, endArrowType: "triangle" } });
slide.addText("充分", { x: 9.68, y: 5.5, w: 0.9, h: 0.26, fontSize: 12, bold: true, color: TEAL_DARK, fontFace: FONT, margin: 0 });
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 6.1, y: 6.15, w: 2.6, h: 0.5, fill: { color: AMBER_TINT }, line: { color: AMBER, width: 1.25 }, rectRadius: 0.07 });
slide.addText("生成答案（≤ 20 块证据）", { x: 6.1, y: 6.15, w: 2.6, h: 0.5, fontSize: 12.5, bold: true, color: "92400E", fontFace: FONT, align: "center", valign: "middle", margin: 0 });
slide.addShape(pres.shapes.LINE, { x: 8.72, y: 6.4, w: 0.14, h: 0, line: { color: MUTED, width: 1.25, endArrowType: "triangle" } });
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 8.9, y: 6.2, w: 1.3, h: 0.4, fill: { color: TEAL_TINT }, line: { color: TEAL, width: 1 }, rectRadius: 0.06 });
slide.addText("答案 + 证据链", { x: 8.9, y: 6.2, w: 1.3, h: 0.4, fontSize: 12, bold: true, color: TEAL_DARK, fontFace: FONT, align: "center", valign: "middle", margin: 0 });

// ---------- 区 C：同题集消融数字 ----------
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 10.65, y: 1.25, w: 2.13, h: 5.55, fill: { color: PANEL }, line: { color: BORDER, width: 1 }, rectRadius: 0.1, shadow: softShadow() });
slide.addText("同题集消融 · EM", { x: 10.8, y: 1.4, w: 1.85, h: 0.3, fontSize: 14, bold: true, color: INK, fontFace: FONT, margin: 0 });
slide.addText("Musique 50 题同题集", { x: 10.8, y: 1.7, w: 1.85, h: 0.26, fontSize: 12, color: MUTED, fontFace: FONT, margin: 0 });

const rows = [
  ["naive", 0.30, GREY_BAR, INK, false],
  ["+ 查询改写", 0.40, TEAL, INK, false],
  ["+ 实体图", 0.40, AMBER, INK, false],
  ["+ 决策层", 0.48, INDIGO, INK, false],
  ["全家桶", 0.50, TEAL_DARK, TEAL_DARK, true],
];
rows.forEach(([label, v, barColor, textColor, bold], i) => {
  const y = 2.06 + i * 0.66;
  slide.addText(label, { x: 10.8, y, w: 1.05, h: 0.26, fontSize: 12, bold, color: textColor, fontFace: FONT, margin: 0 });
  slide.addText(v.toFixed(2), { x: 11.85, y, w: 0.78, h: 0.26, fontSize: 12, bold: true, color: textColor, fontFace: FONT, align: "right", margin: 0 });
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 10.8, y: y + 0.3, w: 1.68, h: 0.17, fill: { color: TRACK }, line: { width: 0 }, rectRadius: 0.08 });
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 10.8, y: y + 0.3, w: 1.68 * (v / 0.52), h: 0.17, fill: { color: barColor }, line: { width: 0 }, rectRadius: 0.08 });
});
slide.addText("证据召回@10", { x: 10.8, y: 5.5, w: 1.85, h: 0.25, fontSize: 12, color: MUTED, fontFace: FONT, margin: 0 });
slide.addText("0.72 → 0.81", { x: 10.8, y: 5.76, w: 1.85, h: 0.3, fontSize: 14, bold: true, color: INDIGO, fontFace: FONT, margin: 0 });
slide.addText("1–3 轮自适应", { x: 10.8, y: 6.14, w: 1.85, h: 0.25, fontSize: 12, color: MUTED, fontFace: FONT, margin: 0 });
slide.addText("早停 37/50 · 空答 2%", { x: 10.8, y: 6.4, w: 1.85, h: 0.25, fontSize: 12, color: MUTED, fontFace: FONT, margin: 0 });

// ---------- 页脚 ----------
slide.addText("基准：MuSiQue dev 分层抽样 50 题 · 行间同题集可比 · 详见 README",
  { x: 0.55, y: 7.05, w: 8.5, h: 0.28, fontSize: 12, color: MUTED, fontFace: FONT, margin: 0 });
slide.addText("github.com/dundundun0116/compassrag", { x: 8.8, y: 7.05, w: 3.98, h: 0.28, fontSize: 12, color: TEAL_DARK, fontFace: FONT, align: "right", margin: 0 });

pres.writeFile({ fileName: "docs/assets/architecture.pptx" }).then(() => console.log("pptx done"));
