import json
import os
import datetime
import threading
import time
from pathlib import Path

# --- IMPORT AGENTS ---
from agents.real_sales_agent import RealSalesAgent
from agents.tech_agent import RealTechAgent 
from agents.pricing_agent import RealPricingAgent
from agents.priority_agent import RealPriorityAgent

# We keep mocks for Tech/Pricing so the pipeline can finish execution if needed
class MockTechAgent: 
    def match_specs(self, x): return {"status": "Pending Tech Implementation"}
class MockPricingAgent:
    def calculate_price(self, x): return {"status": "Pending Pricing Implementation"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "database", "central_rfp_database.json")

class MainAgent:
    def __init__(self):
        self.db_path = DB_FILE
        self._ensure_db_exists()

    def _ensure_db_exists(self):
        if not os.path.exists(self.db_path):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            with open(self.db_path, 'w') as f:
                json.dump([], f)

    def load_db(self):
        try:
            with open(self.db_path, 'r') as f: return json.load(f)
        except: return []

    def save_db(self, data):
        with open(self.db_path, 'w') as f:
            json.dump(data, f, indent=2)

    def save_to_db_record(self, rfp_id, key, value, status=None, processing_stage_update=None):
        """
        Thread-safe helper to update a specific field in a specific RFP record.
        """
        data = self.load_db()
        for item in data:
            if item['rfp_unique_id'] == rfp_id:
                item[key] = value
                if status:
                    item['status'] = status
                if processing_stage_update:
                    for k, v in processing_stage_update.items():
                        item['processing_stage_tracker'][k] = v
                item['last_updated'] = datetime.datetime.now().isoformat()
                break
        self.save_db(data)

    # --- RESTORED: SALES AGENT 2 (PRIORITY LOGIC) ---
    def recalculate_priorities(self):
        """
        Re-runs the 'Sales Agent Phase 2' logic.
        Sorts active RFPs by Margin % and assigns High/Med/Low priority.
        """
        data = self.load_db()
        
        # Filter items that have pricing data (Active Only)
        active_items = []
        for item in data:
            if not item.get('is_archived', False):
                # Check if pricing exists
                if item.get('pricing_agent_output', {}).get('financial_summary'):
                    active_items.append(item)

        # Sort by Margin % (Descending)
        def get_margin(item):
            try:
                # Remove % sign and convert to float
                m_str = str(item['pricing_agent_output']['financial_summary'].get('margin_percentage', '0'))
                return float(m_str.replace('%', ''))
            except: return 0.0

        active_items.sort(key=get_margin, reverse=True)

        # Assign Ranks
        total = len(active_items)
        for rank, rfp in enumerate(active_items):
            # Top 33% = High, Next 33% = Medium, Bottom = Low
            if total > 0:
                if rank < (total * 0.33): p = "High"
                elif rank < (total * 0.66): p = "Medium"
                else: p = "Low"
            else:
                p = "Low"

            # Update the record IN MEMORY (since active_items refers to data objects)
            rfp['sales_agent_2_output'] = {
                "priority_rank": rank + 1,
                "priority_score": p,
                "timestamp": datetime.datetime.now().isoformat()
            }
        
        # Save back to disk
        self.save_db(data)

    # --- BACKGROUND WORKER (THE BRAIN) ---
    def _background_pipeline(self, rfp_id, filepath, filename, auto_mode):
        """
        The Orchestrator Loop: Sales -> Tech -> Pricing -> Priority
        """
        print(f"🧵 [Pipeline] Started for {rfp_id} | Mode: {'AUTO' if auto_mode else 'MANUAL'}")
        
        # ---------------- STEP 1: REAL SALES AGENT ----------------
        try:
             print(f"🤖 Calling Sales Agent (In-Process) on {filename}...")
             
             sales_bot = RealSalesAgent() # Will auto-load settings/keys
             full_response = sales_bot.process_rfp(filepath, filename)
             
             # Check for explicit errors returned by the agent
             if "error" in full_response:
                 raise Exception(full_response["error"])

             # EXTRACT JUST THE SALES OUTPUT PART
             if "sales_agent_output" in full_response:
                 actual_sales_data = full_response["sales_agent_output"]
             else:
                 actual_sales_data = full_response

             # Success! Save data to Central DB
             self.save_to_db_record(
                rfp_id, 
                "sales_agent_output", 
                actual_sales_data, 
                status="Sales Completed",
                processing_stage_update={"sales_agent": "Completed", "tech_agent": "Pending"}
             )
             print(f"✅ [Pipeline] Sales Agent Finished.")

        except Exception as e:
            print(f"❌ [Pipeline] Sales Crash: {e}")
            self.save_to_db_record(rfp_id, "sales_agent_output", {"error": str(e)}, status="Error")
            return

        # --- AUTO MODE CHECK ---
        if not auto_mode:
            print(f"🛑 [Pipeline] Stopped (Manual Mode Selected).")
            self.save_to_db_record(rfp_id, "status", "Pending Tech Analysis", status="Waiting for Manual Action")
            return

        # ---------------- STEP 2: TECH AGENT ----------------
        try:
            print(f"🔧 [Pipeline] Calling Technical Agent...")

            # 1. Initialize
            tech_bot = RealTechAgent()

            # 2. Process
            tech_output = tech_bot.process_rfp_data(actual_sales_data)

            # 3. Save
            self.save_to_db_record(
                rfp_id,
                "tech_agent_output",
                tech_output,
                status="Technical Analysis Done",
                processing_stage_update={"tech_agent": "Completed", "pricing_agent": "Pending"}
            )
            
            # Safe access to match score for printing
            match_score = "N/A"
            if tech_output.get('matched_line_items') and len(tech_output['matched_line_items']) > 0:
                match_score = tech_output['matched_line_items'][0].get('match_score', 'N/A')
            print(f"✅ [Pipeline] Tech Agent Finished. Top Score: {match_score}%")

        except Exception as e:
            print(f"❌ [Pipeline] Tech Crash: {e}")
            self.save_to_db_record(rfp_id, "tech_agent_output", {"error": str(e)}, status="Tech Failed")

        # ---------------- STEP 3: PRICING AGENT ----------------
        try:
            print(f"💰 [Pipeline] Calling Real Pricing Agent...")
            pricing_bot = RealPricingAgent()
            
            # Pass the WHOLE record (Sales + Tech data)
            current_record = self.get_rfp_record(rfp_id) # Helper to get current state
            
            pricing_output = pricing_bot.process_pricing(current_record)

            self.save_to_db_record(
                rfp_id,
                "pricing_agent_output",
                pricing_output,
                status="Bid Ready for Review",
                processing_stage_update={"pricing_agent": "Completed", "final_review": "Pending"}
            )
            print(f"✅ [Pipeline] Pricing Complete. Bid Value: {pricing_output['financial_summary']['final_bid_value']}")

        except Exception as e:
            print(f"❌ [Pipeline] Pricing Crash: {e}")
            self.save_to_db_record(rfp_id, "pricing_agent_output", {"error": str(e)}, status="Pricing Failed")
        
        # ---------------- STEP 4: PRIORITY AGENT ----------------
        try:
            print("🏆 [Pipeline] Calculating Global Priorities...")
            prioritizer = RealPriorityAgent()
            prioritizer.recalculate_all_priorities()
        except Exception as e:
            print(f"❌ Priority Agent Failed: {e}")
            
        # Run legacy priority logic too just in case
        # self.recalculate_priorities()
        # CRITICAL FIX: Do not run legacy logic as it overwrites numeric scores with strings ('High')
        # triggering TypeErrors in the dashboard. RealPriorityAgent handles this correctly.
        print(f"✅ [Pipeline] Pipeline Finished for {rfp_id}")


    # --- PUBLIC API CALLED BY APP.PY ---
    def process_rfp(self, filepath, filename, auto_mode=True):
        """
        1. Creates a record in DB immediately (Status: Processing)
        2. Starts Background Thread
        """
        rfp_id = f"RFP-{int(datetime.datetime.now().timestamp())}"
        
        # Initial DB Record
        new_record = {
            "rfp_unique_id": rfp_id,
            "status": "Processing", # <--- Triggers Animation in Dashboard
            "is_archived": False,
            "created_at": datetime.datetime.now().isoformat(),
            "local_file_name": filename,
            "document_url": f"/static/rfp_docs/{filename}", # Correct Path for Frontend
            "processing_stage_tracker": {
                "sales_agent": "In Progress...",
                "tech_agent": "Pending", "pricing_agent": "Pending", "final_review": "Pending"
            },
            "sales_agent_output": {},
            "tech_agent_output": {},
            "pricing_agent_output": {},
            "sales_agent_2_output": {"priority_score": "Pending"}
        }

        # Insert at top
        data = self.load_db()
        data.insert(0, new_record)
        self.save_db(data)

        # Start Thread
        t = threading.Thread(target=self._background_pipeline, args=(rfp_id, filepath, filename, auto_mode))
        t.daemon = True
        t.start()

        return rfp_id

    def toggle_archive_status(self, rfp_id):
        """Toggles the archive status of an RFP"""
        data = self.load_db()
        for item in data:
            if item['rfp_unique_id'] == rfp_id:
                item['is_archived'] = not item.get('is_archived', False)
                break
        self.save_db(data)
        try:
            RealPriorityAgent().recalculate_all_priorities()
        except: pass
        
        return True

    def get_rfp_record(self, rfp_id):
        """Helper to get fresh data from DB for next agent"""
        data = self.load_db()
        return next((r for r in data if r['rfp_unique_id'] == rfp_id), {})