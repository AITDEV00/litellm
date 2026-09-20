/**
 * Static render-condition extractor for TSX source.
 *
 * For every JSX element <X/> in a .tsx file, determines whether it is gated by
 * a conditional expression ({cond && <X/>}, cond ? <A/> : <B/>) and, if so,
 * records the gating condition text. This bridges the gap between symptom
 * ("TabGroup didn't render") and cause ("gated by usageView === 'global'"):
 * the AST already encodes the cause; this tool reads it out.
 *
 * Output: JSON array of GatingRecord objects, one per gated JSX element.
 * Ungated elements are omitted (they always render when their parent renders).
 *
 * Usage:
 *   node scripts/extract-gating.mjs <glob> [--out <path>] [--include-ungated]
 */
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const dashboardDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function parseArgs(argv) {
  const args = argv.slice(2);
  if (args.length === 0) {
    process.stderr.write("Usage: node scripts/extract-gating.mjs <glob> [--out <path>] [--include-ungated]\n");
    process.exit(1);
  }
  let glob = null;
  let outPath = null;
  let includeUngated = false;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--out") {
      outPath = args[++i];
    } else if (args[i] === "--include-ungated") {
      includeUngated = true;
    } else if (!glob) {
      glob = args[i];
    }
  }
  return { glob, outPath, includeUngated };
}

async function expandGlob(globPattern, cwd) {
  const { glob } = await import("node:fs/promises");
  const results = [];
  for await (const entry of glob(globPattern, { cwd, absolute: true })) {
    results.push(entry);
  }
  return results.sort();
}

function jsxElementName(node) {
  if (ts.isIdentifier(node)) return node.text;
  if (ts.isQualifiedName(node)) return `${jsxElementName(node.left)}.${node.right.text}`;
  if (ts.isPropertyAccessExpression(node)) return `${jsxElementName(node.expression)}.${node.name.text}`;
  if (ts.isJsxNamespacedName(node)) return `${node.namespace.text}:${node.name.text}`;
  return null;
}

function getJsxTagName(openingOrSelfClosing) {
  const tag = openingOrSelfClosing.tagName;
  return jsxElementName(tag);
}

function unwrapParens(node) {
  while (ts.isParenthesizedExpression(node)) {
    node = node.expression;
  }
  return node;
}

function findGatingCondition(openingOrSelfClosing, sf) {
  const isSelfClosing = ts.isJsxSelfClosingElement(openingOrSelfClosing);
  const jsxElement = isSelfClosing ? openingOrSelfClosing : openingOrSelfClosing.parent;
  let current = jsxElement;
  while (current) {
    const parent = current.parent;
    if (!parent) break;
    if (ts.isConditionalExpression(parent) && (parent.whenTrue === current || parent.whenFalse === current)) {
      const branch = parent.whenTrue === current ? "whenTrue" : "whenFalse";
      return {
        kind: "ternary",
        conditionText: unwrapParens(parent.condition).getText(sf),
        branch,
      };
    }
    if (
      ts.isBinaryExpression(parent) &&
      parent.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken &&
      parent.right === current
    ) {
      return {
        kind: "logical-and",
        conditionText: unwrapParens(parent.left).getText(sf),
      };
    }
    if (
      ts.isJsxFragment(current) ||
      ts.isParenthesizedExpression(current) ||
      ts.isJsxExpression(current) ||
      current === jsxElement
    ) {
      if (ts.isJsxElement(parent) || ts.isJsxSelfClosingElement(parent)) {
        break;
      }
      current = parent;
      continue;
    }
    break;
  }
  return null;
}

function findEnclosingJsxElementName(node) {
  let current = node.parent;
  while (current) {
    if (ts.isJsxElement(current)) {
      return getJsxTagName(current.openingElement);
    }
    if (ts.isJsxFragment(current)) {
      return "<>";
    }
    current = current.parent;
  }
  return null;
}

function extractChildElementNames(parentNode, sf) {
  const names = [];
  const children = ts.isJsxElement(parentNode) ? parentNode.children : [];
  for (const child of children) {
    if (ts.isJsxElement(child) || ts.isJsxSelfClosingElement(child)) {
      const opening = ts.isJsxElement(child) ? child.openingElement : child;
      const name = getJsxTagName(opening);
      if (name) names.push(name);
    }
  }
  return names;
}

function extractFromSource(sourceText, filePath, includeUngated = false) {
  const sf = ts.createSourceFile(filePath, sourceText, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const records = [];

  const visit = (node) => {
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
      const opening = ts.isJsxElement(node) ? node.openingElement : node;
      const name = getJsxTagName(opening);
      if (!name) {
        ts.forEachChild(node, visit);
        return;
      }
      const start = sf.getLineAndCharacterOfPosition(opening.getStart(sf));
      const end = sf.getLineAndCharacterOfPosition(node.getEnd());
      const gating = findGatingCondition(opening, sf);
      const enclosingJsx = findEnclosingJsxElementName(opening);
      const childNames = extractChildElementNames(node, sf);

      if (gating || includeUngated) {
        records.push({
          element: name,
          file: relative(dashboardDir, filePath),
          line: start.line + 1,
          endLine: end.line + 1,
          gatedBy: gating ? gating.conditionText : null,
          gateKind: gating ? gating.kind : "none",
          branch: gating && gating.branch ? gating.branch : null,
          enclosingJsx,
          childElements: childNames,
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return records;
}

function extractFromFile(filePath, includeUngated = false) {
  const sourceText = readFileSync(filePath, "utf8");
  return extractFromSource(sourceText, filePath, includeUngated);
}

async function main() {
  const { glob: globPattern, outPath, includeUngated } = parseArgs(process.argv);
  const files = await expandGlob(globPattern, dashboardDir);
  if (files.length === 0) {
    process.stderr.write(`No files matched: ${globPattern}\n`);
    process.exit(1);
  }
  const allRecords = [];
  for (const file of files) {
    const records = extractFromFile(file, includeUngated);
    allRecords.push(...records);
  }
  const output = JSON.stringify(allRecords, null, 2);
  if (outPath) {
    writeFileSync(outPath, output + "\n", "utf8");
    process.stdout.write(`Wrote ${allRecords.length} records to ${outPath}\n`);
  } else {
    process.stdout.write(output + "\n");
  }
}

export { extractFromSource, extractFromFile, findGatingCondition, getJsxTagName };
export default main;

const isDirectRun = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isDirectRun) {
  main().catch((err) => {
    process.stderr.write(`${err.stack || err}\n`);
    process.exit(1);
  });
}
