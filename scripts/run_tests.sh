#!/bin/bash

TOTAL_RUNS=500
PASSED_COUNT=0
SDC_COUNT=0
CRASH_COUNT=0

# Reset iniziale dei log dedicati se vuoi una nuova sessione pulita
> stats_sdc.log
> stats_crash.log

echo "Avvio della campagna di Fault Injection ($TOTAL_RUNS iterazioni)..."

for i in $(seq 1 $TOTAL_RUNS)
do
   # Esegui l'applicazione dirottando sia stdout che stderr a /dev/null per totale silenzio
   LD_LIBRARY_PATH=../../../runtime VORTEX_DRIVER=rtlsim ./vecadd -n64 > /dev/null 2>&1
   STATUS=$?

   if [ $STATUS -eq 0 ]; then
      ((PASSED_COUNT++))
   elif [ $STATUS -eq 1 ]; then
      ((SDC_COUNT++))
   else
      ((CRASH_COUNT++))
   fi

   # --- CALCOLO E STAMPA DELLA BARRA DI PROGRESSO DINAIMCA ---
   PERCENT=$(( i * 100 / TOTAL_RUNS ))
   BARS=$(( PERCENT / 2 )) # 50 barre totali per il 100%
   
   # Costruisci la stringa della barra
   printf -v BAR_STR "%${BARS}s" ""
   BAR_STR="${BAR_STR// /=}"
   
   # Stampa la barra sovrascrivendo la riga precedente (\r)
   # Mostra anche i contatori parziali in tempo reale
   printf "\r[%-50s] %d%% (P: %d | SDC: %d | C: %d)" "$BAR_STR" "$PERCENT" "$PASSED_COUNT" "$SDC_COUNT" "$CRASH_COUNT"
done

echo -e "\n\n=== CAMPAGNA COMPLETATA ==="
echo "PASSED (Masked Faults): $PASSED_COUNT"
echo "SDC (Silent Data Corruption): $SDC_COUNT (Dettagli in stats_sdc.log)"
echo "CRASH / HANG (Timeout): $CRASH_COUNT (Dettagli in stats_crash.log)"