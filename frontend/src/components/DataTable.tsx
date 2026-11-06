import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Search } from "lucide-react";
import { Table, type SortState } from "./ui";

export interface DataColumn<T> {
  key: string;
  label: string;
  render: (row: T) => ReactNode;
  numeric?: boolean;
  /** Value to order by. A column without one is not sortable. */
  sort?: (row: T) => number | string | null | undefined;
}

const PAGE_SIZE = 25;

/**
 * A table that pages, filters and sorts the rows it is given, in the browser.
 *
 * Meant for lists that arrive whole and can be long. Tables whose rows are paged
 * by the server (Opportunities) or that are a handful of evidence rows keep using
 * `Table` directly. Filtering needs `search`, which returns the text a row can be
 * found by; without it the filter box is not shown.
 */
export function DataTable<T>({
  rows,
  columns,
  rowKey,
  caption,
  search,
  defaultSort,
}: {
  rows: T[];
  columns: DataColumn<T>[];
  rowKey: (row: T) => string;
  caption: string;
  search?: (row: T) => string;
  /** Initial order; without it rows keep the order they arrived in. */
  defaultSort?: SortState;
}) {
  const { t, i18n } = useTranslation();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortState | null>(defaultSort ?? null);
  const [requested, setPage] = useState(1);

  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase(i18n.language);
    const found =
      search && needle
        ? rows.filter((r) =>
            search(r).toLocaleLowerCase(i18n.language).includes(needle),
          )
        : rows;
    const column = sort && columns.find((c) => c.key === sort.key)?.sort;
    if (!sort || !column) return found;
    const direction = sort.dir === "asc" ? 1 : -1;
    // Empty values go last in either direction; ties keep their arrival order.
    return found
      .map((row, index) => ({ row, index, value: column(row) }))
      .sort((a, b) => {
        const aEmpty = a.value == null || a.value === "";
        const bEmpty = b.value == null || b.value === "";
        if (aEmpty || bEmpty) return aEmpty === bEmpty ? a.index - b.index : aEmpty ? 1 : -1;
        const order =
          typeof a.value === "number" && typeof b.value === "number"
            ? a.value - b.value
            : String(a.value).localeCompare(String(b.value), i18n.language, {
                numeric: true,
                sensitivity: "base",
              });
        return order * direction || a.index - b.index;
      })
      .map((x) => x.row);
  }, [rows, columns, query, sort, search, i18n.language]);

  const pages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const page = Math.min(requested, pages);
  const onSort = (key: string) => {
    setPage(1);
    // Numbers start with the largest, text with A to Z; a third click clears it.
    const first = columns.find((c) => c.key === key)?.numeric ? "desc" : "asc";
    setSort((current) =>
      current?.key !== key
        ? { key, dir: first }
        : current.dir === first
          ? { key, dir: first === "asc" ? "desc" : "asc" }
          : null,
    );
  };

  return (
    <>
      {search && (
        <div className="table-toolbar">
          <label className="search">
            <Search size={16} aria-hidden="true" />
            <input
              type="search"
              aria-label={`${t("filterRows")}: ${caption}`}
              placeholder={t("filterRows")}
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(1);
              }}
            />
          </label>
          <span role="status">
            {t("rowCount", { shown: visible.length, total: rows.length })}
          </span>
        </div>
      )}
      {visible.length === 0 ? (
        <p className="table-empty">{t("noMatches")}</p>
      ) : (
        <Table
          rows={visible.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)}
          rowKey={rowKey}
          caption={caption}
          sort={sort}
          onSort={onSort}
          columns={columns.map((c) => ({ ...c, sortable: c.sort != null }))}
        />
      )}
      {pages > 1 && (
        <div className="pagination">
          <span>{t("page", { page, pages })}</span>
          <button disabled={page === 1} onClick={() => setPage(page - 1)}>
            {t("previous")}
          </button>
          <button disabled={page === pages} onClick={() => setPage(page + 1)}>
            {t("next")}
          </button>
        </div>
      )}
    </>
  );
}
