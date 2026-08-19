"use client";

import * as React from "react";
import {
  CartesianGrid,
  Line,
  LineChart as RechartsLineChart,
  XAxis,
  YAxis,
  type MouseHandlerDataParam,
} from "recharts";
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, type ChartConfig } from "@/components/ui/chart";
import { cn } from "@/lib/cva.config";
import { ValueTooltip, type ChartTooltipComponent } from "./chart_tooltip";
import { categoryFills, type ChartColor } from "./colors";

export type LineChartCurveType = "linear" | "natural" | "monotone" | "step";

/** Extract the hovered category's data key from a recharts Legend payload, or null. */
function hoveredCategoryKey(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const dataKey = (payload as { dataKey?: unknown }).dataKey;
  return typeof dataKey === "string" ? dataKey : null;
}

export type LineChartProps<TDatum extends Record<string, unknown>> = {
  data: readonly TDatum[];
  index: string;
  categories: readonly string[];
  colors?: readonly ChartColor[];
  valueFormatter?: (value: number) => string;
  yAxisWidth?: number;
  tickGap?: number;
  showLegend?: boolean;
  showXAxis?: boolean;
  showGridLines?: boolean;
  showTooltip?: boolean;
  customTooltip?: ChartTooltipComponent;
  connectNulls?: boolean;
  curveType?: LineChartCurveType;
  className?: string;
  style?: React.CSSProperties;
  /** Invoked when a point on a line is clicked, with the datum row and the category. */
  onPointClick?: (datum: TDatum, category: string) => void;
  /**
   * When many categories are drawn (e.g. historical model performance with a
   * model per line), hovering a line or a legend key focuses that category
   * (full opacity, wider stroke) and fades every other line to grey so the
   * user can read a single series. Defaults to off.
   */
  highlightOnHover?: boolean;
};

export function LineChart<TDatum extends Record<string, unknown>>({
  data,
  index,
  categories,
  colors,
  valueFormatter,
  yAxisWidth = 56,
  tickGap = 5,
  showLegend = true,
  showXAxis = true,
  showGridLines = true,
  showTooltip = true,
  customTooltip,
  connectNulls = false,
  curveType = "linear",
  className,
  style,
  onPointClick,
  highlightOnHover = false,
}: LineChartProps<TDatum>) {
  const fills = categoryFills(categories.length, colors);
  const config: ChartConfig = Object.fromEntries(categories.map((category) => [category, { label: category }]));
  const TooltipContent = customTooltip ?? ValueTooltip;
  const [activeCategory, setActiveCategory] = React.useState<string | null>(null);

  const isFocused = (category: string): boolean =>
    Boolean(highlightOnHover && activeCategory !== null && activeCategory === category);
  const isDimmed = (category: string): boolean =>
    Boolean(highlightOnHover && activeCategory !== null && activeCategory !== category);
  const lineWidth = (category: string): number => {
    if (isDimmed(category)) return 1;
    if (isFocused(category)) return 3;
    return 2;
  };

  return (
    <ChartContainer config={config} className={cn("aspect-auto h-80 w-full", className)} style={style}>
      <RechartsLineChart
        data={[...data]}
        onClick={
          onPointClick
            ? (state: MouseHandlerDataParam) => {
                const index = state.activeIndex;
                const row = typeof index === "number" ? data[index] : undefined;
                const dataKey = state.activeDataKey;
                if (row && dataKey) onPointClick(row, String(dataKey));
              }
            : undefined
        }
      >
        {showGridLines && <CartesianGrid vertical={false} />}
        <XAxis
          dataKey={index}
          hide={!showXAxis}
          tickLine={false}
          axisLine={false}
          minTickGap={tickGap}
          interval="equidistantPreserveStart"
        />
        <YAxis width={yAxisWidth} tickLine={false} axisLine={false} tickFormatter={valueFormatter} />
        {showTooltip && (
          <ChartTooltip
            content={({ active, payload, label }) => (
              <TooltipContent
                active={active}
                payload={payload}
                label={label}
                {...(customTooltip ? {} : { valueFormatter })}
              />
            )}
          />
        )}
        {showLegend && (
          <ChartLegend
            verticalAlign="top"
            onMouseEnter={(payload) => {
              const dataKey = hoveredCategoryKey(payload);
              if (dataKey !== null) setActiveCategory(dataKey);
            }}
            onMouseLeave={() => {
              if (highlightOnHover) setActiveCategory(null);
            }}
            content={
              <ChartLegendContent
                className="justify-end text-muted-foreground"
                onKeyMouseEnter={(key) => setActiveCategory(key)}
                onKeyMouseLeave={() => setActiveCategory(null)}
              />
            }
          />
        )}
        {categories.map((category, i) => (
          <Line
            key={category}
            type={curveType}
            dataKey={category}
            stroke={fills[i]}
            strokeWidth={lineWidth(category)}
            strokeOpacity={isDimmed(category) ? 0.15 : 1}
            dot={false}
            isAnimationActive={false}
            connectNulls={connectNulls}
            onMouseEnter={
              highlightOnHover
                ? () => {
                    setActiveCategory(category);
                  }
                : undefined
            }
            onMouseLeave={
              highlightOnHover
                ? () => {
                    setActiveCategory(null);
                  }
                : undefined
            }
          />
        ))}
      </RechartsLineChart>
    </ChartContainer>
  );
}
