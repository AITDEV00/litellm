import React from "react";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from "@/components/ui/dropdown-menu";
import { ArrowUpDown, ChevronUp, ChevronDown, X } from "lucide-react";

export type SortState = "asc" | "desc" | false;

interface TableHeaderSortDropdownProps {
  /**
   * Current sort state: "asc", "desc", or false for neutral
   */
  sortState: SortState;
  /**
   * Callback when sort state changes
   * @param newState - The new sort state: "asc", "desc", or false
   */
  onSortChange: (newState: SortState) => void;
  /**
   * Optional column ID for identification
   */
  columnId?: string;
}

export const TableHeaderSortDropdown: React.FC<TableHeaderSortDropdownProps> = ({ sortState, onSortChange }) => {
  const current = sortState === false ? "reset" : sortState;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        onClick={(e) => e.stopPropagation()}
        className={sortState ? "text-blue-500 hover:text-blue-600" : "text-gray-400 hover:text-blue-500"}
        aria-label="Sort column"
      >
        {sortState === "asc" ? (
          <ChevronUp className="size-4" />
        ) : sortState === "desc" ? (
          <ChevronDown className="size-4" />
        ) : (
          <ArrowUpDown className="size-4" />
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" onClick={(e) => e.stopPropagation()}>
        <DropdownMenuRadioGroup
          value={current}
          onValueChange={(key) => {
            if (key === "asc") {
              onSortChange("asc");
            } else if (key === "desc") {
              onSortChange("desc");
            } else {
              onSortChange(false);
            }
          }}
        >
          <DropdownMenuRadioItem value="asc">
            <ChevronUp className="size-4" /> Ascending
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="desc">
            <ChevronDown className="size-4" /> Descending
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="reset">
            <X className="size-4" /> Reset
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
};
