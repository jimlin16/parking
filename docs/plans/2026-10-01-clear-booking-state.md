# Clear Booking State Implementation Plan

**Goal:** Clear the current saved reservation's pending marker with one click, without a confirmation prompt.

**Architecture:** Share the booking identity calculation between submission and reset. Serialize reset against dashboard scanning, booking, and the cross-process booking lock. Keep other reservations and logs intact.

**Tech Stack:** Python, unittest, vanilla JavaScript.

1. Extract the pending-marker path helper in booking_safety.py.
2. Add Dashboard.clear_booking_state and POST /api/booking/reset; reject active operations.
3. Add a button beside immediate booking, save edited reservation settings before resetting, and disable it during active operations.
4. Test targeted deletion, repeated clearing, active locks, cross-process locking, and HTTP routing. Run the existing unittest suite.
