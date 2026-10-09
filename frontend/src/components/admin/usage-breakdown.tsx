"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface UsageBreakdownProps {
  queriesByDay: Array<Record<string, unknown>>;
  queriesByUser: Array<Record<string, unknown>>;
}

export function UsageBreakdown({ queriesByDay, queriesByUser }: UsageBreakdownProps) {
  const renderTable = (
    title: string,
    data: Array<Record<string, unknown>>,
    emptyMessage: string,
  ) => {
    if (data.length === 0) {
      return (
        <Card>
          <CardHeader>
            <CardTitle>{title}</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted">{emptyMessage}</p>
          </CardContent>
        </Card>
      );
    }

    const keys = Object.keys(data[0]);
    return (
      <Card>
        <CardHeader>
          <CardTitle>{title}</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-border bg-surface">
                <tr>
                  {keys.map((key) => (
                    <th
                      key={key}
                      className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-muted"
                    >
                      {key}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.map((row, idx) => (
                  <tr key={idx} className="hover:bg-surface/50">
                    {keys.map((key) => (
                      <td key={key} className="px-4 py-3 text-sm text-foreground">
                        {String(row[key] ?? "")}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    );
  };

  return (
    <div className="space-y-6">
      {renderTable(
        "Queries by day",
        queriesByDay,
        "No daily breakdown yet — question history is empty.",
      )}
      {renderTable(
        "Queries by user",
        queriesByUser,
        "No user breakdown yet — question history is empty.",
      )}
    </div>
  );
}
