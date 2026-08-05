try:
    import supabase
    from supabase import create_client
    print("✅ Supabase import successful!")
    print(f"Supabase version: {supabase.__version__}")
except ImportError as e:
    print(f"❌ Supabase import failed: {e}")