// The audit of this app runs against the REAL API on a COPY of the database (audit/serve-test.sh), so nothing is faked: unknown routes fall through.
module.exports = () => null;
