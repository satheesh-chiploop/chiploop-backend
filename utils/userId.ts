import { createClientComponentClient } from "@supabase/auth-helpers-nextjs";

export async function getStableUserId() {
  const supabase = createClientComponentClient();
  const { data: { session } } = await supabase.auth.getSession();
  return session?.user?.id || localStorage.getItem("anon_user_id") || "anonymous";
}
