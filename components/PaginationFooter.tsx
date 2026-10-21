import { ChevronLeft, ChevronRight } from "lucide-react";

export default function PaginationFooter({
  page,
  pageSize,
  total,
  onPageChange,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const visibleCount = Math.min(5, totalPages);
  const firstPage = Math.max(1, Math.min(page - 2, totalPages - visibleCount + 1));
  const pages = Array.from({ length: visibleCount }, (_, index) => firstPage + index);
  const rangeStart = total ? (page - 1) * pageSize + 1 : 0;
  const rangeEnd = Math.min(page * pageSize, total);

  return (
    <div className="panel-footer panel-footer--pagination">
      <p>Exibindo <strong>{rangeStart}-{rangeEnd}</strong> de <strong>{total}</strong></p>
      <nav className="pagination" aria-label="Paginação de produtos">
        <button type="button" onClick={() => onPageChange(page - 1)} disabled={page <= 1} aria-label="Página anterior">
          <ChevronLeft size={15} />
        </button>
        {pages.map((pageNumber) => (
          <button
            key={pageNumber}
            type="button"
            className={pageNumber === page ? "pagination__page pagination__page--active" : "pagination__page"}
            onClick={() => onPageChange(pageNumber)}
            aria-current={pageNumber === page ? "page" : undefined}
          >
            {pageNumber}
          </button>
        ))}
        <button type="button" onClick={() => onPageChange(page + 1)} disabled={page >= totalPages} aria-label="Próxima página">
          <ChevronRight size={15} />
        </button>
      </nav>
    </div>
  );
}
