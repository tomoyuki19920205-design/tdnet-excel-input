import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import AlertsPage from "@/components/tdnet-alerts/AlertsPage";

export const metadata = {
  title: "TDNET Alerts | イベント一覧",
};

export default async function TdnetAlertsPage() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  return <AlertsPage userId={user.id} userEmail={user.email || ""} />;
}
