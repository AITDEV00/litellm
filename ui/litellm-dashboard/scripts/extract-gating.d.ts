export type GateKind = "logical-and" | "ternary" | "none";

export type Branch = "whenTrue" | "whenFalse" | null;

export interface GatingRecord {
  element: string;
  file: string;
  line: number;
  endLine: number;
  gatedBy: string | null;
  gateKind: GateKind;
  branch: Branch;
  enclosingJsx: string | null;
  childElements: string[];
}

export interface GatingCondition {
  kind: "logical-and" | "ternary";
  conditionText: string;
  branch?: Branch;
}

export declare function extractFromSource(
  sourceText: string,
  filePath: string,
  includeUngated?: boolean,
): GatingRecord[];

export declare function extractFromFile(filePath: string, includeUngated?: boolean): GatingRecord[];

export declare function findGatingCondition(openingOrSelfClosing: unknown, sf: unknown): GatingCondition | null;

export declare function getJsxTagName(node: unknown): string | null;

declare const _default: () => Promise<void>;
export default _default;
