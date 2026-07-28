import { describe, it, expect } from "vitest";
import { extractFromSource } from "../scripts/extract-gating.mjs";

const wrap = (jsx: string) => `
const Cond = () => null;
const Foo = () => null;
const Bar = () => null;
const Baz = () => null;
const Tab = ({children}: {children?: React.ReactNode}) => <>{children}</>;
const TabGroup = ({children}: {children?: React.ReactNode}) => <>{children}</>;
const TabList = ({children}: {children?: React.ReactNode}) => <>{children}</>;
const Comp = () => (
  <div>
    ${jsx}
  </div>
);
`;

const findRecord = (records: ReturnType<typeof extractFromSource>, element: string) =>
  records.find((r) => r.element === element);

describe("extractFromSource", () => {
  it("extracts a logical-and gate: {cond && <Foo/>}", () => {
    const records = extractFromSource(wrap("{showFoo && <Foo/>}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo).toBeDefined();
    expect(foo!.gatedBy).toBe("showFoo");
    expect(foo!.gateKind).toBe("logical-and");
    expect(foo!.branch).toBeNull();
  });

  it("extracts a ternary true-branch: {cond ? <Foo/> : <Bar/>}", () => {
    const records = extractFromSource(wrap("{flag ? <Foo/> : <Bar/>}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo).toBeDefined();
    expect(foo!.gatedBy).toBe("flag");
    expect(foo!.gateKind).toBe("ternary");
    expect(foo!.branch).toBe("whenTrue");
  });

  it("extracts a ternary false-branch: {cond ? <Foo/> : <Bar/>}", () => {
    const records = extractFromSource(wrap("{flag ? <Foo/> : <Bar/>}"), "test.tsx");
    const bar = findRecord(records, "Bar");
    expect(bar).toBeDefined();
    expect(bar!.gatedBy).toBe("flag");
    expect(bar!.gateKind).toBe("ternary");
    expect(bar!.branch).toBe("whenFalse");
  });

  it("extracts a compound condition: {a === 'x' && <Foo/>}", () => {
    const records = extractFromSource(wrap("{view === 'global' && <Foo/>}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo!.gatedBy).toBe("view === 'global'");
  });

  it("extracts a logical-or condition: {(a || b) && <Foo/>}", () => {
    const records = extractFromSource(wrap("{(view === 'global' || view === 'my') && <Foo/>}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo!.gatedBy).toBe("view === 'global' || view === 'my'");
    expect(foo!.gateKind).toBe("logical-and");
  });

  it("extracts a gate wrapping a JSX fragment: {cond && (<> <Foo/> <Bar/> </>)}", () => {
    const records = extractFromSource(wrap("{showAll && (<><Foo/><Bar/></>)}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo).toBeDefined();
    expect(foo!.gatedBy).toBe("showAll");
    const bar = findRecord(records, "Bar");
    expect(bar).toBeDefined();
    expect(bar!.gatedBy).toBe("showAll");
  });

  it("does not record descendants of a gated element", () => {
    const records = extractFromSource(wrap("{loading && <Foo><Bar/><Baz/></Foo>}"), "test.tsx");
    expect(findRecord(records, "Foo")).toBeDefined();
    expect(findRecord(records, "Bar")).toBeUndefined();
    expect(findRecord(records, "Baz")).toBeUndefined();
  });

  it("records childElements of a gated element", () => {
    const records = extractFromSource(wrap("{show && <Foo><Bar/><Baz/></Foo>}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo!.childElements).toEqual(["Bar", "Baz"]);
  });

  it("records childElements of a TabGroup with tabs", () => {
    const records = extractFromSource(
      wrap("{show && <TabGroup><TabList><Tab>A</Tab></TabList></TabGroup>}"),
      "test.tsx",
    );
    const tg = findRecord(records, "TabGroup");
    expect(tg).toBeDefined();
    expect(tg!.childElements).toEqual(["TabList"]);
  });

  it("omits ungated elements by default", () => {
    const records = extractFromSource(wrap("<Foo/>"), "test.tsx");
    expect(findRecord(records, "Foo")).toBeUndefined();
  });

  it("includes ungated elements when includeUngated=true", () => {
    const records = extractFromSource(wrap("<Foo/>"), "test.tsx", true);
    const foo = findRecord(records, "Foo");
    expect(foo).toBeDefined();
    expect(foo!.gatedBy).toBeNull();
    expect(foo!.gateKind).toBe("none");
  });

  it("extracts self-closing elements: {cond && <Foo/>}", () => {
    const records = extractFromSource(wrap("{show && <Foo />}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo).toBeDefined();
    expect(foo!.gatedBy).toBe("show");
  });

  it("extracts deeply nested gates: {outer && <div>{inner && <Foo/>}</div>}", () => {
    const records = extractFromSource(wrap("{outer && <div>{inner && <Foo/>}</div>}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo).toBeDefined();
    expect(foo!.gatedBy).toBe("inner");
  });

  it("extracts a gate inside a parenthesized expression: {cond && (<Foo/>)}", () => {
    const records = extractFromSource(wrap("{show && (<Foo/>)}"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo).toBeDefined();
    expect(foo!.gatedBy).toBe("show");
  });

  it("records the enclosing JSX element name", () => {
    const records = extractFromSource(wrap("<div>{show && <Foo/>}</div>"), "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo!.enclosingJsx).toBe("div");
  });

  it("records correct line numbers", () => {
    const source = `const Comp = () => (
  <div>
    {show && <Foo/>}
  </div>
);`;
    const records = extractFromSource(source, "test.tsx");
    const foo = findRecord(records, "Foo");
    expect(foo!.line).toBe(3);
  });

  it("handles multiple gated siblings at the same level", () => {
    const records = extractFromSource(wrap("{a && <Foo/>}{b && <Bar/>}"), "test.tsx");
    expect(findRecord(records, "Foo")!.gatedBy).toBe("a");
    expect(findRecord(records, "Bar")!.gatedBy).toBe("b");
  });

  it("handles a ternary wrapping a fragment in false branch", () => {
    const records = extractFromSource(wrap("{flag ? <Foo/> : (<><Bar/><Baz/></>)}"), "test.tsx");
    expect(findRecord(records, "Foo")!.branch).toBe("whenTrue");
    expect(findRecord(records, "Bar")!.branch).toBe("whenFalse");
    expect(findRecord(records, "Baz")!.branch).toBe("whenFalse");
  });

  it("handles a nested ternary: {a ? (b ? <Foo/> : <Bar/>) : <Baz/>}", () => {
    const records = extractFromSource(wrap("{a ? (b ? <Foo/> : <Bar/>) : <Baz/>}"), "test.tsx");
    expect(findRecord(records, "Foo")!.gatedBy).toBe("b");
    expect(findRecord(records, "Foo")!.branch).toBe("whenTrue");
    expect(findRecord(records, "Baz")!.gatedBy).toBe("a");
    expect(findRecord(records, "Baz")!.branch).toBe("whenFalse");
  });

  it("extracts the real tab bug pattern: fragment-gated TabGroup with tabs", () => {
    const source = `const Comp = () => (
  <div>
    {(view === "global" || view === "my-usage") && (
      <>
        <TabGroup>
          <TabList>
            <Tab>Cost</Tab>
            <Tab>Model Analytics</Tab>
            <Tab>Real-Time Per Model</Tab>
          </TabList>
        </TabGroup>
      </>
    )}
    {view === "customer" && <Baz/>}
  </div>
);`;
    const records = extractFromSource(source, "test.tsx");
    const tg = findRecord(records, "TabGroup");
    expect(tg).toBeDefined();
    expect(tg!.gatedBy).toBe('view === "global" || view === "my-usage"');
    expect(tg!.childElements).toEqual(["TabList"]);

    const baz = findRecord(records, "Baz");
    expect(baz!.gatedBy).toBe('view === "customer"');
  });
});
