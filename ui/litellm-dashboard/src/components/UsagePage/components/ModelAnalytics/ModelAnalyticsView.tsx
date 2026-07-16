import { useQuery } from "@tanstack/react-query";
import {
  BarChart,
  Card,
  Col,
  Grid,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
  Title,
} from "@tremor/react";
import { Alert, Select } from "antd";
import React, { useMemo, useState } from "react";

import {
  modelExceptionsCall,
  modelLatencyMetricsCall,
  modelSlowResponsesCall,
  modelStreamingMetricsCall,
  type ExceptionsResponse,
  type ExceptionsRow,
  type ModelLatencyMetricsResponse,
  type ModelStreamingMetricsResponse,
  type SlowResponsesRow,
} from "../../../networking";
import { ChartLoader } from "../../../shared/chart_loader";

interface ModelAnalyticsViewProps {
  accessToken: string | null;
  modelGroups: string[];
  startTime: Date | null;
  endTime: Date | null;
}

const formatSeconds = (value: number) => {
  if (value < 1) return `${(value * 1000).toFixed(0)}ms`;
  return `${value.toFixed(2)}s`;
};

const ModelAnalyticsView: React.FC<ModelAnalyticsViewProps> = ({ accessToken, modelGroups, startTime, endTime }) => {
  const [selectedModelGroup, setSelectedModelGroup] = useState<string | undefined>(modelGroups[0]);

  const queryParams = useMemo(
    () => ({ modelGroup: selectedModelGroup, startTime, endTime }),
    [selectedModelGroup, startTime, endTime],
  );

  const streamingQuery = useQuery<ModelStreamingMetricsResponse>({
    queryKey: ["modelStreamingMetrics", queryParams],
    queryFn: () => modelStreamingMetricsCall(accessToken!, queryParams),
    enabled: Boolean(accessToken && selectedModelGroup && startTime && endTime),
  });

  const latencyQuery = useQuery<ModelLatencyMetricsResponse>({
    queryKey: ["modelLatencyMetrics", queryParams],
    queryFn: () => modelLatencyMetricsCall(accessToken!, queryParams),
    enabled: Boolean(accessToken && selectedModelGroup && startTime && endTime),
  });

  const slowQuery = useQuery<SlowResponsesRow[]>({
    queryKey: ["modelSlowResponses", queryParams],
    queryFn: () => modelSlowResponsesCall(accessToken!, queryParams),
    enabled: Boolean(accessToken && selectedModelGroup && startTime && endTime),
  });

  const exceptionsQuery = useQuery<ExceptionsResponse>({
    queryKey: ["modelExceptions", queryParams],
    queryFn: () => modelExceptionsCall(accessToken!, queryParams),
    enabled: Boolean(accessToken && selectedModelGroup && startTime && endTime),
  });

  const isLoading =
    streamingQuery.isLoading || latencyQuery.isLoading || slowQuery.isLoading || exceptionsQuery.isLoading;
  const hasError = streamingQuery.isError || latencyQuery.isError || slowQuery.isError || exceptionsQuery.isError;

  const streamingData = streamingQuery.data?.data ?? [];
  const streamingCategories = streamingQuery.data?.all_api_bases ?? [];
  const latencyData = latencyQuery.data?.data ?? [];
  const latencyCategories = latencyQuery.data?.all_api_bases ?? [];
  const slowData = slowQuery.data ?? [];
  const exceptionsData = exceptionsQuery.data?.data ?? [];
  const exceptionTypes = exceptionsQuery.data?.exception_types ?? [];

  if (!startTime || !endTime) {
    return (
      <Card>
        <Alert message="Select a date range to view model analytics." type="info" showIcon />
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <span className="text-sm font-medium text-gray-700">Model Group:</span>
        <Select
          style={{ width: 300 }}
          value={selectedModelGroup}
          onChange={(value: string) => setSelectedModelGroup(value)}
          placeholder="Select a model group"
          options={modelGroups.map((g) => ({ label: g, value: g }))}
          showSearch
        />
      </div>

      {hasError && (
        <Alert
          message="Failed to load some metrics. The proxy may not have spend logs for this model group in the selected window."
          type="warning"
          showIcon
        />
      )}

      <Grid numItems={2} className="gap-4">
        <Col numColSpan={2}>
          <Card>
            <Title>Time to First Token (seconds) over time</Title>
            {isLoading ? (
              <ChartLoader />
            ) : streamingData.length === 0 ? (
              <p className="text-gray-500 text-sm py-8 text-center">No streaming data for this model group.</p>
            ) : (
              <BarChart
                data={streamingData}
                index="date"
                categories={streamingCategories}
                colors={["cyan", "blue", "indigo", "violet"]}
                valueFormatter={formatSeconds}
                yAxisWidth={80}
                stack={false}
              />
            )}
          </Card>
        </Col>

        <Col numColSpan={2}>
          <Card>
            <Title>Avg Latency per Token (seconds/token) over time</Title>
            {isLoading ? (
              <ChartLoader />
            ) : latencyData.length === 0 ? (
              <p className="text-gray-500 text-sm py-8 text-center">No latency data for this model group.</p>
            ) : (
              <BarChart
                data={latencyData}
                index="date"
                categories={latencyCategories}
                colors={["emerald", "teal", "green", "lime"]}
                valueFormatter={formatSeconds}
                yAxisWidth={80}
                stack={false}
              />
            )}
          </Card>
        </Col>

        <Col numColSpan={1}>
          <Card className="h-full">
            <Title>Slow Responses per API Base</Title>
            {isLoading ? (
              <ChartLoader />
            ) : slowData.length === 0 ? (
              <p className="text-gray-500 text-sm py-8 text-center">No slow response data.</p>
            ) : (
              <Table>
                <TableHead>
                  <TableRow>
                    <TableHeaderCell>API Base</TableHeaderCell>
                    <TableHeaderCell>Total</TableHeaderCell>
                    <TableHeaderCell>Slow</TableHeaderCell>
                    <TableHeaderCell>% Slow</TableHeaderCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {slowData.map((row: SlowResponsesRow) => {
                    const pct = row.total_count > 0 ? ((row.slow_count / row.total_count) * 100).toFixed(1) : "0.0";
                    return (
                      <TableRow key={row.api_base}>
                        <TableCell>{row.api_base || "(unknown)"}</TableCell>
                        <TableCell>{row.total_count}</TableCell>
                        <TableCell>{row.slow_count}</TableCell>
                        <TableCell>{pct}%</TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </Card>
        </Col>

        <Col numColSpan={1}>
          <Card className="h-full">
            <Title>Exceptions per API Base</Title>
            {isLoading ? (
              <ChartLoader />
            ) : exceptionsData.length === 0 ? (
              <p className="text-gray-500 text-sm py-8 text-center">No exception data.</p>
            ) : (
              <Table>
                <TableHead>
                  <TableRow>
                    <TableHeaderCell>Model / API Base</TableHeaderCell>
                    <TableHeaderCell>Total</TableHeaderCell>
                    {exceptionTypes.map((et: string) => (
                      <TableHeaderCell key={et}>{et}</TableHeaderCell>
                    ))}
                  </TableRow>
                </TableHead>
                <TableBody>
                  {exceptionsData.map((row: ExceptionsRow) => (
                    <TableRow key={row.model}>
                      <TableCell>{row.model || "(unknown)"}</TableCell>
                      <TableCell>{row.total_exceptions}</TableCell>
                      {exceptionTypes.map((et: string) => (
                        <TableCell key={et}>{(row[et] as number) ?? 0}</TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Card>
        </Col>
      </Grid>
    </div>
  );
};

export default ModelAnalyticsView;
