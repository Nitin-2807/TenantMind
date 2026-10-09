import { Spinner } from "@/components/ui/spinner";

export default function AppLoading() {
  return (
    <div
      className="flex min-h-[50vh] items-center justify-center"
      role="status"
      aria-label="Loading"
    >
      <Spinner size="lg" />
    </div>
  );
}